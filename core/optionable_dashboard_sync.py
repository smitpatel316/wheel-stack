"""
Optionable dashboard push — engine observability (capital card + scan funnel +
open positions).

Self-contained module added 2026-08-14 (Smit-approved, inspired by the
AllYouNeedIsWheel review). Attached at the start of a run, it tees stdout AND
stderr so it sees both print() lines ([DATA]/[BP]/[FUND]... in core/strategy.py)
and logger lines ([ACCOUNT]/[SGOV]/Selling put...), parses the known formats
into a structured snapshot + scan funnel, and POSTs them to Optionable at the
end of the run:

    POST {OPTIONABLE_URL}/api/engine/dashboard
    { "snapshot": {...}, "scanRun": {...} }

Schema (must match optionable-src/src/components/engine/EnginePanels.jsx):
- snapshot: equity, cash, optionsBuyingPower, riskUsed, riskCap, sgovShares,
  sgovValue, sgovMonthlyYield, regime, vix
- scanRun: slot, contractsConsidered, aggregateRejects {reason: n},
  symbols: [{symbol, dropReason?, contractsConsidered?, rejects?, action,
             detail?}]   action in sold|skipped|none

Usage (see docs/dashboard-sync.patch):
    from core.optionable_dashboard_sync import EngineDashboardPush
    dash = EngineDashboardPush(); dash.install()          # run start
    ...
    dash.push(client, symbols_all, allowed_symbols, slot) # run end

Read/display only — never places orders. Fail-safe: any error here must never
break the trading run, so every public method swallows exceptions after logging.
"""
import datetime
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger("strategy.optionable_dashboard_sync")

OPTIONABLE_URL = os.getenv("OPTIONABLE_URL", "http://localhost:8096")
TIMEOUT = 8

_ET = ZoneInfo("America/New_York")

_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
# The dashboard moved off this host (Pi migration 2026-08-27); its income and
# benchmark charts need the equity/SGOV history that used to sit beside it on
# disk, so the history now rides the regular dashboard push. Cap entries to
# stay well under the API's JSON body limit.
_HISTORY_MAX_ENTRIES = 1000


def _read_history(filename: str, value_key: str) -> List[dict]:
    """Load a logs/*.json history file, trimmed and sanitized. Never raises;
    returns [] on any problem — history is best-effort display data."""
    try:
        raw = json.loads((_LOGS_DIR / filename).read_text())
        if not isinstance(raw, list):
            return []
        return [
            {"t": e["t"], value_key: float(e[value_key]), **({"avg": e["avg"]} if e.get("avg") is not None else {})}
            for e in raw[-_HISTORY_MAX_ENTRIES:]
            if isinstance(e, dict) and e.get("t") and isinstance(e.get(value_key), (int, float))
        ]
    except Exception as e:
        logger.debug("[SWALLOWED] history read for dash push failed: %r", e)
        return []


class _TeeStream:
    """Passes writes through to the real stream and mirrors lines to the collector."""

    def __init__(self, real, on_line):
        self._real = real
        self._on_line = on_line
        self._buf = ""

    def write(self, s):
        try:
            self._real.write(s)
        except Exception as e:
            logger.debug("[SWALLOWED] tee write to real stream failed: %r", e)
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            try:
                self._on_line(line)
            except Exception as e:
                logger.debug("[SWALLOWED] dashboard line parse failed for %r: %r", line[:120], e)
                pass

    def flush(self):
        try:
            self._real.flush()
        except Exception as e:
            logger.debug("[SWALLOWED] tee flush to real stream failed: %r", e)
            pass

    def __getattr__(self, name):  # isatty, fileno, etc.
        return getattr(self._real, name)


_RE_ACCOUNT = re.compile(
    r"\[ACCOUNT\] Equity \$([\d.]+) Cash \$([\d.]+) Stock BP \$([\d.]+) Options BP \$([\d.]+) Risk \$([\d.]+)/(\d+)")
_RE_CONTEXT = re.compile(r"\[CONTEXT\] Regime=(\w+) VIX=([\d.]+)")
_RE_SGOV_TARGET = re.compile(r"\[SGOV\] target (\d+) shares \$(\d+)")
_RE_SGOV_YIELD = re.compile(r"earning \$([\d.]+)/mo")
_RE_UNDERLYING = re.compile(r"\[DATA\] underlying filter: (\d+) in -> (\d+) after price/BP")
_RE_BP_DROP = re.compile(r"\[BP\] Dropped over BP limit \$\d+: \[(.*)\]")
_RE_BP_PAIR = re.compile(r"\('([A-Z.]+)'")
_RE_FUND = re.compile(r"\[FUND\] Skip ([A-Z.]+): (.+)")
_RE_EARNINGS = re.compile(r"\[EARNINGS\] Skip ([A-Z.]+): (.+)")
_RE_DIVIDEND = re.compile(r"\[DIVIDEND\] Skip (?:call )?([A-Z.]+): (.+)")
_RE_LIQ = re.compile(r"\[LIQ\] Drying detected: \[(.*)\]")
_RE_LIQ_SYM = re.compile(r"'([A-Z.]+)'")
_RE_REJECTS = re.compile(r"\[DATA\] option filter rejected all (\d+) contracts: (\{.*\})")
_RE_REJECT_PAIR = re.compile(r"'(\w+)': (\d+)")
_RE_LOGGED_CSP = re.compile(r"Optionable: logged CSP ([A-Z]+) \$([\d.]+) exp ([\d-]+)")
_RE_SELL_FAILED = re.compile(r"Sell failed for ([A-Z]+\d{6}[PC]\d+)")
_RE_SKIP_OBP = re.compile(r"Skipping ([A-Z]+\d{6}[PC]\d+) strike \$([\d.]+): needs \$([\d.]+) > Alpaca options BP \$([\d.]+)")
_RE_SKIP_BP = re.compile(r"Skipping ([A-Z]+\d{6}[PC]\d+) strike \$([\d.]+) need \$([\d.]+) > BP \$([\d.]+)")
_RE_OCC = re.compile(r"^([A-Z]+)\d{6}[PC]")


# ---------- open positions (Open Positions table, added 2026-08-22) ----------


def _fnum(val, default=0.0):
    """Float-or-default; never raises."""
    try:
        f = float(val)
        return f if f == f else default  # NaN guard
    except Exception as e:
        # swallow:intentional - broker payload fields can be None/odd; default is correct
        logger.debug("[SWALLOWED] _fnum(%r) -> default %s: %r", val, default, e)
        return default


def _underlying_price(client, symbol):
    """Latest trade price for an underlying via the engine's data client.
    Returns None on any failure — never raises."""
    try:
        res = client.get_stock_latest_trade(symbol)
        trade = res.get(symbol) if hasattr(res, "get") else res
        price = getattr(trade, "price", None)
        return float(price) if price else None
    except Exception as e:
        logger.debug("[SWALLOWED] latest-trade fetch for %s failed (otmPct will be null): %r", symbol, e)
        return None


def collect_open_positions(client, roll_counts=None, funding_entries=None,
                           today=None, price_fetcher=None) -> Tuple[list, list]:
    """Build the openPositions + fundingQueue payload pieces for the dashboard.

    Row shape (must match optionable-src/src/components/positions/OpenPositionsTable.jsx):
      symbol, underlying, type (CSP|CC|SGOV|STOCK|OPT), label?, strike, expiry,
      dte, contracts, entryPrice, currentPrice, marketValue, unrealizedPL,
      unrealizedPLpct, otmPct (positive = out of the money, negative = ITM;
      null when the underlying price can't be fetched), rollsUsed?, rollsMax?

    Failure-isolated like the rest of this module: a bad position is skipped
    with a [SWALLOWED] log, and total failure returns ([], []). Never raises.
    """
    from core.optionable_sync import _parse_occ  # local import: keeps module load light
    try:
        if today is None:
            today = datetime.datetime.now(_ET).date()
        if roll_counts is None:
            try:
                from core.state_manager import load_roll_counts
                roll_counts = load_roll_counts()
            except Exception as e:
                logger.debug("[SWALLOWED] roll-count load for dashboard positions failed: %r", e)
                roll_counts = {}
        if funding_entries is None:
            try:
                from core.funding_queue import FundingQueue
                funding_entries = list(FundingQueue().load().entries)
            except Exception as e:
                logger.debug("[SWALLOWED] funding-queue load for dashboard positions failed: %r", e)
                funding_entries = []
        fetch_price = price_fetcher or (lambda sym: _underlying_price(client, sym))

        rows = []
        for p in (client.get_positions() or []):
            try:
                sym = str(getattr(p, "symbol", "") or "").strip()
                if not sym:
                    continue
                qty = _fnum(getattr(p, "qty", 0))
                raw_side = getattr(p, "side", "") or ""
                # alpaca-py returns a PositionSide enum whose str() is
                # "PositionSide.SHORT" — read .value when present.
                side = str(getattr(raw_side, "value", raw_side)).lower()
                is_short = side == "short" or qty < 0
                row = {
                    "symbol": sym,
                    "underlying": sym,
                    "type": "STOCK",
                    "strike": None,
                    "expiry": None,
                    "dte": None,
                    "contracts": abs(int(qty)),
                    "entryPrice": _fnum(getattr(p, "avg_entry_price", 0)),
                    "currentPrice": _fnum(getattr(p, "current_price", 0)),
                    "marketValue": _fnum(getattr(p, "market_value", 0)),
                    "unrealizedPL": _fnum(getattr(p, "unrealized_pl", 0)),
                    "unrealizedPLpct": _fnum(getattr(p, "unrealized_plpc", 0)) * 100,
                    "otmPct": None,
                }
                parsed = _parse_occ(sym) if _RE_OCC.match(sym) else None
                if parsed:
                    underlying, expiry_iso, opt_type, strike, _ = parsed
                    row["underlying"] = underlying
                    row["strike"] = strike
                    row["expiry"] = expiry_iso
                    try:
                        row["dte"] = (datetime.date.fromisoformat(expiry_iso) - today).days
                    except Exception as e:
                        logger.debug("[SWALLOWED] dte math for %s failed: %r", sym, e)
                    if is_short:
                        row["type"] = "CSP" if opt_type == "Put" else "CC"
                    else:
                        row["type"] = "OPT"  # long option — the wheel never holds these
                    if row["type"] in ("CSP", "CC") and strike:
                        u = fetch_price(underlying)
                        if u:
                            if row["type"] == "CSP":
                                row["otmPct"] = round((u - strike) / u * 100, 2)
                            else:
                                row["otmPct"] = round((strike - u) / u * 100, 2)
                elif sym == "SGOV":
                    row["type"] = "SGOV"
                    row["label"] = "cash sweep"
                # roll usage: lineage key matches underlying + option side
                if row["type"] == "CSP":
                    used = roll_counts.get(f"{row['underlying']}:P")
                elif row["type"] == "CC":
                    used = roll_counts.get(f"{row['underlying']}:C")
                else:
                    used = None
                if used:
                    row["rollsUsed"] = int(used)
                    row["rollsMax"] = 2  # MAX_ROLLS_PER_LINEAGE
                rows.append(row)
            except Exception as e:
                logger.warning("[SWALLOWED] dashboard position row build failed for %r: %r",
                               getattr(p, "symbol", "?"), e)
                continue

        queue_rows = []
        for e in funding_entries:
            try:
                queue_rows.append({
                    "symbol": e.get("symbol"),
                    "underlying": e.get("underlying"),
                    "strike": e.get("strike"),
                    "expiry": e.get("expiration"),
                    "need": e.get("need"),
                    "queued_at": e.get("queued_at"),
                    "valid_for": e.get("valid_for"),
                })
            except Exception as ex:
                logger.debug("[SWALLOWED] funding-queue row build failed: %r", ex)
                continue
        return rows, queue_rows
    except Exception as e:
        logger.warning("[DASH] collect_open_positions failed (non-fatal): %r", e)
        return [], []


class EngineDashboardPush:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = base_url or OPTIONABLE_URL
        self.snapshot = {}
        self.drop_reasons = {}   # symbol -> reason
        self.actions = {}        # symbol -> (action, detail)
        self.underlying_in = None
        self.underlying_out = None
        self.aggregate_rejects = {}
        self.contracts_rejected_total = 0
        self._orig_stdout = None
        self._orig_stderr = None

    # ---- lifecycle ----

    def install(self):
        """Tee stdout/stderr so printed and logged lines are both parsed.

        Also rebinds any StreamHandler the 'strategy' logger (or its children)
        already attached to the real stdout/stderr: logging.StreamHandler
        captures the stream object at construction time, so handlers created
        before install() would keep writing to the original stream and their
        lines ([ACCOUNT], [CONTEXT], [SGOV] — the entire snapshot) would never
        reach the parser. print() resolves sys.stdout dynamically and is not
        affected, which is why the scan funnel worked while the snapshot
        stayed empty.
        """
        try:
            if self._orig_stdout is None:
                self._orig_stdout, self._orig_stderr = sys.stdout, sys.stderr
                sys.stdout = _TeeStream(self._orig_stdout, self._on_line)
                sys.stderr = _TeeStream(self._orig_stderr, self._on_line)
            self._rebind_logger_streams()
        except Exception as e:
            logger.warning(f"[DASH] install failed: {e}")

    def _rebind_logger_streams(self):
        """Point pre-existing 'strategy' StreamHandlers at the tee streams."""
        try:
            strat = logging.getLogger("strategy")
            for log in [strat] + [
                logging.getLogger(n) for n in list(logging.Logger.manager.loggerDict)
                if isinstance(n, str) and n.startswith("strategy")
            ]:
                for h in getattr(log, "handlers", []):
                    if not isinstance(h, logging.StreamHandler):
                        continue
                    if h.stream is self._orig_stdout:
                        h.setStream(sys.stdout)
                    elif h.stream is self._orig_stderr:
                        h.setStream(sys.stderr)
        except Exception as e:
            logger.debug("[SWALLOWED] logger stream rebind failed: %r", e)

    def uninstall(self):
        try:
            if self._orig_stdout is not None:
                sys.stdout, sys.stderr = self._orig_stdout, self._orig_stderr
                self._orig_stdout = self._orig_stderr = None
        except Exception as e:
            logger.debug("[SWALLOWED] dashboard uninstall (restore stdout/stderr) failed: %r", e)
            pass

    # ---- parsing ----

    def _on_line(self, line: str):
        m = _RE_ACCOUNT.search(line)
        if m:
            self.snapshot.update({
                "equity": float(m.group(1)), "cash": float(m.group(2)),
                "optionsBuyingPower": float(m.group(4)),
                "riskUsed": float(m.group(5)), "riskCap": float(m.group(6)),
            })
            return
        m = _RE_CONTEXT.search(line)
        if m:
            self.snapshot.update({"regime": m.group(1), "vix": float(m.group(2))})
            return
        m = _RE_SGOV_TARGET.search(line)
        if m:
            self.snapshot.update({"sgovShares": int(m.group(1)), "sgovValue": float(m.group(2))})
            return
        m = _RE_SGOV_YIELD.search(line)
        if m:
            self.snapshot["sgovMonthlyYield"] = float(m.group(1))
            return
        m = _RE_UNDERLYING.search(line)
        if m:
            self.underlying_in, self.underlying_out = int(m.group(1)), int(m.group(2))
            return
        m = _RE_BP_DROP.search(line)
        if m:
            for sym in _RE_BP_PAIR.findall(m.group(1)):
                self.drop_reasons.setdefault(sym, "over BP limit")
            return
        for rx, prefix in ((_RE_FUND, "fundamentals"), (_RE_EARNINGS, "earnings"), (_RE_DIVIDEND, "dividend")):
            m = rx.search(line)
            if m:
                reason = m.group(2).strip().rstrip("]")
                self.drop_reasons.setdefault(m.group(1), f"{prefix}: {reason}")
                return
        m = _RE_LIQ.search(line)
        if m:
            for sym in _RE_LIQ_SYM.findall(m.group(1)):
                self.drop_reasons.setdefault(sym, "liquidity: volume drying")
            return
        m = _RE_REJECTS.search(line)
        if m:
            self.contracts_rejected_total += int(m.group(1))
            for k, v in _RE_REJECT_PAIR.findall(m.group(2)):
                self.aggregate_rejects[k] = self.aggregate_rejects.get(k, 0) + int(v)
            return
        m = _RE_LOGGED_CSP.search(line)
        if m:
            # Only a successful fill reaches Optionable logging — the
            # "Selling put:" line fires before the order attempt.
            try:
                exp = m.group(3)
                detail = f"${m.group(2)}P {exp[5:7]}/{exp[8:10]}"
            except Exception as e:
                logger.debug("[SWALLOWED] CSP detail date formatting failed, using strike only: %r", e)
                detail = f"${m.group(2)}P"
            self.actions[m.group(1)] = ("sold", detail)
            return
        m = _RE_SELL_FAILED.search(line)
        if m:
            om = _RE_OCC.match(m.group(1))
            if om:
                self.actions[om.group(1)] = ("skipped", "order failed")
            return
        m = _RE_SKIP_OBP.search(line)
        if m:
            om = _RE_OCC.match(m.group(1))
            if om:
                self.actions.setdefault(om.group(1), ("skipped", f"needs ${m.group(3)} > options BP"))
            return
        m = _RE_SKIP_BP.search(line)
        if m:
            om = _RE_OCC.match(m.group(1))
            if om:
                self.actions.setdefault(om.group(1), ("skipped", f"needs ${m.group(3)} > risk BP"))
            return

    # ---- push ----

    def push(self, client=None, symbols_all: Optional[List[str]] = None,
             allowed_symbols: Optional[List[str]] = None, slot: str = ""):
        """POST the snapshot + scan funnel to Optionable. Never raises."""
        try:
            symbols_all = list(symbols_all or [])
            allowed = set(allowed_symbols or [])
            rows = []
            seen = set()
            for sym in symbols_all:
                seen.add(sym)
                row = {"symbol": sym}
                if sym in self.drop_reasons:
                    row["dropReason"] = self.drop_reasons[sym]
                elif allowed and sym not in allowed:
                    row["dropReason"] = "filtered"
                if sym in self.actions:
                    row["action"], row["detail"] = self.actions[sym]
                rows.append(row)
            # symbols seen in logs but not in the passed-in universe list
            for sym in sorted(set(self.drop_reasons) | set(self.actions) - seen):
                if sym not in seen:
                    row = {"symbol": sym}
                    if sym in self.drop_reasons:
                        row["dropReason"] = self.drop_reasons[sym]
                    if sym in self.actions:
                        row["action"], row["detail"] = self.actions[sym]
                    rows.append(row)

            scan_run = {
                "slot": slot,
                "contractsConsidered": self.contracts_rejected_total or None,
                "aggregateRejects": self.aggregate_rejects or None,
                "symbols": rows,
            }
            payload = {}
            if self.snapshot:
                payload["snapshot"] = self.snapshot
            if rows or self.aggregate_rejects:
                payload["scanRun"] = scan_run
            # Open positions + funding queue for the Open Positions table.
            open_positions, funding_queue = [], []
            if client is not None:
                open_positions, funding_queue = collect_open_positions(client)
                payload["openPositions"] = open_positions
                payload["fundingQueue"] = funding_queue
            if not payload:
                logger.info("[DASH] nothing collected this run - skipping push")
                return False
            equity_hist = _read_history("equity_history.json", "equity")
            if equity_hist:
                payload["equityHistory"] = equity_hist
            sgov_hist = _read_history("sgov_history.json", "shares")
            if sgov_hist:
                payload["sgovHistory"] = sgov_hist
            r = requests.post(f"{self.base_url}/api/engine/dashboard", json=payload, timeout=TIMEOUT)
            if r.status_code == 200:
                logger.info(f"[DASH] pushed snapshot+scan funnel ({len(rows)} symbols, {len(open_positions)} positions, {len(equity_hist)} equity pts) to Optionable")
                return True
            body = (r.text or "").replace("\n", " ")[:120]
            logger.warning(f"[DASH] Optionable push HTTP {r.status_code}: {body}")
            return False
        except Exception as e:
            logger.warning(f"[DASH] push failed (non-fatal): {e}")
            return False


def push_now(snapshot: dict = None, scan_run: dict = None, base_url: Optional[str] = None,
             include_history: bool = False):
    """One-shot push for callers that already have structured data.
    Set include_history=True to also seed the equity/SGOV history blobs
    (used for the Pi migration seed; runs get history automatically)."""
    try:
        payload = {}
        if snapshot:
            payload["snapshot"] = snapshot
        if scan_run:
            payload["scanRun"] = scan_run
        if include_history:
            equity_hist = _read_history("equity_history.json", "equity")
            if equity_hist:
                payload["equityHistory"] = equity_hist
            sgov_hist = _read_history("sgov_history.json", "shares")
            if sgov_hist:
                payload["sgovHistory"] = sgov_hist
        if not payload:
            return False
        r = requests.post(f"{base_url or OPTIONABLE_URL}/api/engine/dashboard",
                          json=payload, timeout=TIMEOUT)
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"[DASH] push_now failed (non-fatal): {e}")
        return False
