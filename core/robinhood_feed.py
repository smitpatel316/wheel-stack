"""Read-only Robinhood market-data sidecar for the wheel (shadow validation).

Purpose (Smit, 2026-08-17): run the wheel on Alpaca paper while validating
Robinhood as the future live venue. For every CSP candidate the engine
scores, fetch the matching Robinhood option quote and log Alpaca vs RH
side-by-side; also compare underlying quotes for the whole watchlist.

HARD RULES:
- Observation only. Nothing here may influence a trade decision; Alpaca
  remains the source of truth. This module has no order-path callers.
- READ-ONLY forever: it shells out to rh_mcp_client.py, whose own `call`
  command refuses place_*/cancel_*/buy/sell tools, and it only ever requests
  get_* tools. There is no write path to Robinhood from this repo.
- Fail soft: any RH/subprocess/parse failure logs and returns None; the run
  continues on Alpaca data alone. After MAX_FAILURES consecutive failures the
  feed disables itself for the rest of the run.

Each `call` is a fresh MCP session (~3-8s), so usage is bounded by
MAX_CALLS_PER_RUN and a per-underlying chain cache. Tests mock _run_cli.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(f"strategy.{__name__}")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CLIENT_DIR = Path("~/workspace/robinhood-mcp").expanduser()
ET = ZoneInfo("America/New_York")

MAX_FAILURES = 3
MAX_CALLS_PER_RUN = 40
CALL_TIMEOUT = 90  # seconds per MCP subprocess


def default_log_path(now: datetime | None = None) -> Path:
    now = now or datetime.now(ET)
    return REPO_ROOT / "logs" / f"rh-compare-{now.strftime('%Y%m%d')}.jsonl"


def _f(v) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None  # NaN guard
    except (TypeError, ValueError):
        return None


class RobinhoodFeed:
    def __init__(self, client_dir: Path | None = None, log_path: Path | None = None,
                 enabled: bool = True, log=None):
        self.client_dir = client_dir or DEFAULT_CLIENT_DIR
        self.python = self.client_dir / "venv" / "bin" / "python"
        self.script = self.client_dir / "rh_mcp_client.py"
        self.tokens = self.client_dir / "tokens.json"
        self.log = log or logger
        self.enabled = bool(enabled)
        self.disabled_reason = None
        self.log_path = log_path or Path(
            os.environ.get("RH_COMPARE_LOG", str(default_log_path())))
        if self.enabled and not self.python.exists():
            self.enabled = False
            self.disabled_reason = f"missing venv python at {self.python}"
        if self.enabled and not self.tokens.exists():
            self.enabled = False
            self.disabled_reason = "no tokens.json - run rh_mcp_client.py auth/finish"
        # per-run state
        self._chain_cache: dict[str, list[dict] | None] = {}
        self._calls = 0
        self._failures = 0
        self.stats = {"candidates": 0, "matched": 0, "diffs": [],
                      "und_compared": 0, "und_matched": 0, "und_diffs": []}

    @classmethod
    def from_env(cls, log=None) -> "RobinhoodFeed":
        enabled = os.getenv("RH_COMPARE_ENABLED", "true").lower() in ("1", "true", "yes")
        feed = cls(enabled=enabled, log=log)
        if not enabled:
            feed.disabled_reason = "RH_COMPARE_ENABLED off"
        return feed

    # ---- transport ----
    def _run_cli(self, tool: str, args: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.python), str(self.script), "call", tool, json.dumps(args)],
            capture_output=True, text=True, timeout=CALL_TIMEOUT,
            cwd=str(self.client_dir),
        )

    def _call(self, tool: str, args: dict) -> dict | None:
        """One MCP tool call -> the tool's data payload, or None on any failure."""
        if not self.enabled:
            return None
        if self._failures >= MAX_FAILURES:
            if self.enabled:
                self.enabled = False
                self.disabled_reason = f"{MAX_FAILURES} consecutive failures"
                self.log.warning(f"[RH] disabling feed for this run: {self.disabled_reason}")
            return None
        if self._calls >= MAX_CALLS_PER_RUN:
            return None
        self._calls += 1
        try:
            proc = self._run_cli(tool, args)
            if proc.returncode != 0:
                raise RuntimeError(f"exit {proc.returncode}: {(proc.stderr or '')[-200:]}")
            d = json.loads(proc.stdout)
            if d.get("is_error"):
                raise RuntimeError(f"tool error: {json.dumps(d.get('content'))[:200]}")
            self._failures = 0
            return (d.get("structured_content") or {}).get("data")
        except Exception as e:
            self._failures += 1
            self.log.warning(f"[RH] {tool} failed ({self._failures}/{MAX_FAILURES}): {e}")
            return None

    # ---- market data ----
    def get_equity_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """symbol -> {bid, ask, last} (floats; missing fields omitted)."""
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), 20):  # RH closes lookup caps at 20
            data = self._call("get_equity_quotes", {"symbols": symbols[i:i + 20]})
            for r in (data or {}).get("results", []):
                q = r.get("quote") or {}
                sym = q.get("symbol")
                if not sym:
                    continue
                entry = {}
                for k_src, k_dst in (("bid_price", "bid"), ("ask_price", "ask"),
                                     ("last_trade_price", "last")):
                    v = _f(q.get(k_src))
                    if v is not None:
                        entry[k_dst] = v
                out[sym] = entry
        return out

    def _chain(self, underlying: str) -> list[dict] | None:
        if underlying in self._chain_cache:
            return self._chain_cache[underlying]
        data = self._call("get_option_chains", {"underlying_symbol": underlying})
        chains = (data or {}).get("chains") if data else None
        self._chain_cache[underlying] = chains
        return chains

    def find_put_quote(self, underlying: str, strike: float,
                       expiration: str | None) -> dict | None:
        """RH bid/ask for the put matching underlying+strike+expiration.

        Chain -> instruments (filtered by date/strike/type) -> quote.
        Returns {bid, ask, instrument_id} or None when unmatched/unavailable.
        """
        if not underlying or strike is None or not expiration:
            return None
        exp = str(expiration)[:10]
        chains = self._chain(underlying)
        if not chains:
            return None
        chain = None
        for c in chains:
            if exp in (c.get("expiration_dates") or []):
                chain = c
                break
        if not chain:
            return None
        data = self._call("get_option_instruments", {
            "chain_id": chain["id"],
            "expiration_dates": exp,
            "strike_price": f"{float(strike):.4f}",
            "type": "put",
            "state": "active",
        })
        instruments = (data or {}).get("instruments") if data else None
        if not instruments:
            return None
        inst = instruments[0]
        qdata = self._call("get_option_quotes", {"instrument_ids": [inst["id"]]})
        results = (qdata or {}).get("results") if qdata else None
        if not results:
            return None
        q = results[0].get("quote") or {}
        bid, ask = _f(q.get("bid_price")), _f(q.get("ask_price"))
        if bid is None and ask is None:
            return None
        return {"bid": bid, "ask": ask, "instrument_id": inst.get("id"),
                "rh_symbol": inst.get("chain_symbol")}

    # ---- comparison logging (observation only) ----
    def _append(self, line: dict) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(json.dumps(line, sort_keys=True) + "\n")
        except Exception as e:
            self.log.debug(f"[RH] compare log write failed: {e}")

    def compare_put(self, contract) -> dict:
        """Side-by-side Alpaca vs RH for one candidate Contract. Never raises
        into the caller's decision path: on RH failure we still log the
        Alpaca side with rh=null."""
        self.stats["candidates"] += 1
        a_bid, a_ask = _f(contract.bid_price), _f(contract.ask_price)
        a_mid = (a_bid + a_ask) / 2 if a_bid and a_ask else (a_bid or a_ask)
        rh = None
        try:
            rh = self.find_put_quote(contract.underlying, contract.strike,
                                     contract.expiration)
        except Exception as e:
            self.log.debug(f"[RH] put lookup failed for {contract.symbol}: {e}")
        rh_mid = None
        if rh:
            rb, ra = rh.get("bid"), rh.get("ask")
            rh_mid = (rb + ra) / 2 if rb and ra else (rb or ra)
        diff_pct = None
        if a_mid and rh_mid:
            diff_pct = (rh_mid - a_mid) / a_mid * 100.0
            self.stats["matched"] += 1
            self.stats["diffs"].append(diff_pct)
        line = {
            "t": datetime.now(ET).isoformat(timespec="seconds"),
            "kind": "candidate",
            "symbol": contract.symbol,
            "underlying": contract.underlying,
            "strike": contract.strike,
            "expiration": str(contract.expiration)[:10] if contract.expiration else None,
            "alpaca": {"bid": a_bid, "ask": a_ask},
            "rh": ({"bid": rh.get("bid"), "ask": rh.get("ask")} if rh else None),
            "alpaca_mid": a_mid,
            "rh_mid": rh_mid,
            "mid_diff_pct": round(diff_pct, 2) if diff_pct is not None else None,
        }
        self._append(line)
        if rh_mid is not None:
            self.log.info(f"[RH CMP] {contract.symbol} Alpaca {a_bid}/{a_ask} mid ${a_mid:.2f} vs RH {rh.get('bid')}/{rh.get('ask')} mid ${rh_mid:.2f} (diff {diff_pct:+.1f}%)")
        else:
            self.log.info(f"[RH CMP] {contract.symbol} Alpaca {a_bid}/{a_ask} mid ${a_mid:.2f} - no RH match")
        return line

    def compare_underlyings(self, alpaca_prices: dict[str, float]) -> list[dict]:
        """alpaca_prices: symbol -> last price. Batch RH quotes and log diffs."""
        rh_quotes = self.get_equity_quotes(list(alpaca_prices))
        lines = []
        for sym, a_px in alpaca_prices.items():
            self.stats["und_compared"] += 1
            rq = rh_quotes.get(sym)
            rh_last = (rq or {}).get("last")
            diff_pct = None
            if a_px and rh_last:
                diff_pct = (rh_last - a_px) / a_px * 100.0
                self.stats["und_matched"] += 1
                self.stats["und_diffs"].append(diff_pct)
            line = {"t": datetime.now(ET).isoformat(timespec="seconds"),
                    "kind": "underlying", "symbol": sym,
                    "alpaca_price": a_px, "rh_last": rh_last,
                    "diff_pct": round(diff_pct, 3) if diff_pct is not None else None}
            self._append(line)
            lines.append(line)
        return lines

    # ---- context cross-checks (added 2026-08-18 hardening) ----
    def compare_earnings(self, finnhub_dates: dict[str, str], days: int = 14) -> list[dict]:
        """Cross-check Finnhub earnings dates against Robinhood's calendar.

        finnhub_dates: symbol -> 'YYYY-MM-DD' (the engine's earnings_map).
        Logs kind='earnings' records: agree / rh_only (RH has a date the
        engine doesn't — potential blind spot) / finnhub_only / date_mismatch.
        """
        data = self._call("get_earnings_calendar",
                          {"days": days, "filter": "high_market_cap"})
        rh_dates: dict[str, str] = {}
        for r in (data or {}).get("results", []):
            sym, rep = r.get("symbol"), r.get("report") or {}
            if sym and rep.get("date"):
                rh_dates[sym] = rep["date"]
        lines = []
        for sym in sorted(set(finnhub_dates) | set(rh_dates)):
            fd, rd = finnhub_dates.get(sym), rh_dates.get(sym)
            if fd and rd and fd == rd:
                continue  # agree; don't spam the log
            kind = ("date_mismatch" if fd and rd else
                    "rh_only" if rd else "finnhub_only")
            line = {"t": datetime.now(ET).isoformat(timespec="seconds"),
                    "kind": "earnings", "symbol": sym, "check": kind,
                    "finnhub_date": fd, "rh_date": rd}
            self._append(line)
            lines.append(line)
            self.log.warning(f"[RH EARN] {sym} {kind}: finnhub={fd} rh={rd}")
        self.log.info(f"[RH EARN] cross-checked {len(finnhub_dates) + len(rh_dates)} symbols, "
                      f"{len(lines)} discrepancies")
        return lines

    def compare_fundamentals(self, alpha_report: dict[str, dict]) -> list[dict]:
        """Cross-check Alpha Vantage fundamentals against Robinhood.

        alpha_report: symbol -> fundamentals_report entry (uses data.PERatio /
        data.PriceToBookRatio / data.MarketCapitalization when present).
        Divergence threshold: 25% relative on any compared field.
        """
        symbols = list(alpha_report)
        rh_data: dict[str, dict] = {}
        for i in range(0, len(symbols), 10):  # RH caps at 10/call
            data = self._call("get_equity_fundamentals",
                              {"symbols": symbols[i:i + 10]})
            for r in (data or {}).get("results", []):
                if r.get("symbol"):
                    rh_data[r["symbol"]] = r
        lines = []
        for sym, rep in alpha_report.items():
            rh_row = rh_data.get(sym)
            if not rh_row:
                continue
            av_data = rep.get("data") or {}
            fields = {"pe": (av_data.get("PERatio"), rh_row.get("pe_ratio")),
                      "pb": (av_data.get("PriceToBookRatio"), rh_row.get("pb_ratio")),
                      "market_cap": (av_data.get("MarketCapitalization"), rh_row.get("market_cap"))}
            for field, (a_v, r_v) in fields.items():
                a_f, r_f = _f(a_v), _f(r_v)
                if not a_f or not r_f:
                    continue
                diff_pct = (r_f - a_f) / a_f * 100.0
                if abs(diff_pct) > 25:
                    line = {"t": datetime.now(ET).isoformat(timespec="seconds"),
                            "kind": "fundamentals", "symbol": sym, "field": field,
                            "alpha": a_f, "rh": r_f, "diff_pct": round(diff_pct, 1)}
                    self._append(line)
                    lines.append(line)
                    self.log.warning(f"[RH FUND] {sym} {field}: alpha={a_f} rh={r_f} ({diff_pct:+.0f}%)")
        self.log.info(f"[RH FUND] cross-checked {len(rh_data)}/{len(symbols)} symbols, "
                      f"{len(lines)} divergences")
        return lines

    def get_vix(self) -> float | None:
        """VIX level from Robinhood indexes (2 calls: symbol -> id -> quote)."""
        data = self._call("get_indexes", {"symbols": "VIX"})
        idx = ((data or {}).get("indexes") or [{}])[0]
        iid = idx.get("id")
        if not iid:
            return None
        qdata = self._call("get_index_quotes", {"instrument_ids": [iid]})
        quotes = (qdata or {}).get("quotes") or []
        return _f(quotes[0].get("value")) if quotes else None

    def compare_vix(self, engine_vix: float | None) -> dict | None:
        rh_vix = self.get_vix()
        if rh_vix is None or not engine_vix:
            return None
        diff = rh_vix - engine_vix
        line = {"t": datetime.now(ET).isoformat(timespec="seconds"),
                "kind": "vix", "engine": engine_vix, "rh": rh_vix,
                "diff": round(diff, 2)}
        self._append(line)
        lvl = self.log.warning if abs(diff) > 1.5 else self.log.info
        lvl(f"[RH VIX] engine={engine_vix} rh={rh_vix} (diff {diff:+.2f})")
        return line

    def summary(self) -> str:
        s = self.stats

        def med(vals):
            return f"{statistics.median(vals):+.2f}%" if vals else "n/a"

        return (f"RH compare: {s['candidates']} candidates, {s['matched']} matched, "
                f"median mid diff {med(s['diffs'])} | underlyings "
                f"{s['und_matched']}/{s['und_compared']} matched, median diff "
                f"{med(s['und_diffs'])} | calls {self._calls}, failures {self._failures}"
                + (f" (disabled: {self.disabled_reason})" if self.disabled_reason else ""))
