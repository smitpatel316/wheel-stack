"""Next-day funding queue for CSP candidates blocked by T+1 settled-cash BP.

Why this exists (2026-08-17): Alpaca options buying power counts SETTLED cash
only (T+1). Selling SGOV same-day to fund a CSP never frees options BP in
time — the candidate gets skipped and the sweep immediately buys the SGOV
back. In the Aug 17 15:05 ET run that produced five pointless sell+buy
round-trips (~$165k churn). Smit's approved fix: when options BP can't cover
a candidate THIS run, skip it, queue it for next-day funding, and pre-fund
the whole queue with ONE SGOV sale per run. Next day's runs re-validate each
queued candidate through the normal scan (the queue is hints only, never a
blind order list) and fund from the now-settled cash.

File layout (state/funding_queue.json, overridable via WHEEL_FUNDING_QUEUE):
{
  "entries": [
    {"symbol": "F260918P00014000", "underlying": "F", "strike": 14.0,
     "expiration": "2026-09-18", "need": 1400.0, "score": 0.012,
     "queued_at": "2026-08-17T15:05:11-04:00", "valid_for": "2026-08-18"}
  ],
  "prefunded": 1400.0   // dollars of SGOV already sold to cover the queue
}

Safety properties:
- entries expire after 1 trading day (valid_for = next weekday; market
  holidays just expire unused — safe direction)
- loading a corrupt file returns an EMPTY queue AND marks the file broken so
  callers must not pre-fund (never sell SGOV off unknown state)
- the prefunded ledger prevents a second run the same day from selling SGOV
  again for the same entries
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(f"strategy.{__name__}")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = REPO_ROOT / "state" / "funding_queue.json"


def queue_path() -> Path:
    return Path(os.environ.get("WHEEL_FUNDING_QUEUE", str(DEFAULT_PATH)))


def next_trading_day(d: date) -> date:
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:  # Sat/Sun; holidays expire unused (safe)
        nxt += timedelta(days=1)
    return nxt


class FundingQueue:
    def __init__(self, path: Path | None = None, today: date | None = None):
        self.path = path or queue_path()
        self.today = today or date.today()
        self.entries: list[dict] = []
        self.prefunded: float = 0.0
        self.last_prefund: dict | None = None  # {"qty", "at"} of latest pre-fund sale
        self.broken = False   # corrupt file -> read-only mode, never pre-fund
        self.dirty = False

    # ---- persistence ----
    def load(self) -> "FundingQueue":
        if not self.path.exists():
            return self
        try:
            data = json.loads(self.path.read_text())
            self.entries = list(data.get("entries", []))
            self.prefunded = float(data.get("prefunded", 0.0) or 0.0)
            self.last_prefund = data.get("last_prefund") or None
        except Exception as e:
            logger.warning(f"[FUND QUEUE] corrupt {self.path}: {e} - treating as empty, pre-funding DISABLED this run")
            self.entries = []
            self.prefunded = 0.0
            self.broken = True
            return self
        self.dedupe_by_underlying()
        return self

    def dedupe_by_underlying(self) -> list[dict]:
        """Keep only the newest entry per underlying (last one wins).

        add() replaces stale same-underlying entries, but only when a NEW
        candidate for that underlying is queued. State written before that
        fix (or left by runs whose fresh candidates were all blocked by the
        headroom cap) can hold several entries for one underlying — each run
        re-scans fresh and only ever sells ONE contract per underlying, so
        every older entry is dead weight that over-reserves cash against the
        SGOV sweep (2026-08-19: three AAPL entries = $87k of a $134k queue
        reserved against $650 of real risk headroom, sweep pinned at 0).
        Mirrors add(): the prefunded dollar ledger is untouched because any
        SGOV already sold stays credited to the queue.
        """
        newest_idx: dict[str, int] = {}
        for i, e in enumerate(self.entries):
            newest_idx[e.get("underlying", "")] = i
        kept, dropped = [], []
        for i, e in enumerate(self.entries):
            (kept if newest_idx[e.get("underlying", "")] == i else dropped).append(e)
        if dropped:
            for e in dropped:
                logger.info(f"[FUND QUEUE] dedupe: dropped stale {e.get('symbol')} (${float(e.get('need', 0)):.0f}) for {e.get('underlying')} - newer entry kept")
            self.entries = kept
            self.dirty = True
        return dropped

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(
                {"entries": self.entries, "prefunded": round(self.prefunded, 2),
                 "last_prefund": self.last_prefund},
                indent=2, sort_keys=True))
            os.replace(tmp, self.path)
            self.dirty = False
        except Exception as e:
            logger.warning(f"[FUND QUEUE] save failed: {e}")

    # ---- entry lifecycle ----
    def _live(self, e: dict) -> bool:
        try:
            return date.fromisoformat(str(e.get("valid_for", ""))) >= self.today
        except ValueError as exc:
            logger.warning("[SWALLOWED] funding queue entry %s has bad valid_for %r, treating as expired: %r", e.get("symbol"), e.get("valid_for"), exc)
            return False

    def expire(self) -> list[dict]:
        dropped = [e for e in self.entries if not self._live(e)]
        if dropped:
            for e in dropped:
                logger.info(f"[FUND QUEUE] expired unfunded: {e.get('symbol')} (queued {e.get('queued_at', '?')[:10]})")
                self.prefunded = max(0.0, self.prefunded - float(e.get("need", 0) or 0))
            self.entries = [e for e in self.entries if self._live(e)]
            self.dirty = True
        return dropped

    def add(self, symbol: str, underlying: str, strike: float,
            expiration: str | None, need: float, score: float = 0.0) -> bool:
        """Queue a candidate for next-day funding. Dedupes by OCC symbol.

        Also REPLACES any older entry for the same underlying: each run
        re-scans fresh and only ever sells ONE contract per underlying
        (select_options is best-per-underlying), so keeping stale
        strikes/expiries over-reserves cash and over-prefunds SGOV sales
        (2026-08-18: three AAPL entries from three runs reserved ~$87k
        against ~$42k of real risk headroom). The prefunded ledger is
        dollars, not per-contract, so a replaced entry's already-sold cash
        correctly stays credited to the queue.
        """
        if any(e.get("symbol") == symbol for e in self.entries):
            return False
        stale = [e for e in self.entries
                 if e.get("underlying") == underlying and e.get("symbol") != symbol]
        if stale:
            for e in stale:
                logger.info(f"[FUND QUEUE] replacing stale {e.get('symbol')} (${float(e.get('need', 0)):.0f}) with fresh {symbol} (${need:.0f}) for {underlying}")
            stale_ids = {id(e) for e in stale}
            self.entries = [e for e in self.entries if id(e) not in stale_ids]
            self.dirty = True
        self.entries.append({
            "symbol": symbol,
            "underlying": underlying,
            "strike": strike,
            "expiration": expiration,
            "need": need,
            "score": score,
            "queued_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "valid_for": next_trading_day(self.today).isoformat(),
        })
        self.dirty = True
        return True

    def mark_filled(self, symbol: str, underlying: str | None = None) -> bool:
        """Candidate sold (funded from settled cash) -> drop from queue.

        When `underlying` is given, also drops any OTHER queued entries for
        the same underlying: a fill means we now hold a CSP on that name, so
        reserving cash for a second contract on it is moot. Fresh scans pick
        strike/expiry each run, so exact-symbol matching alone almost never
        fires (2026-08-19: AAPL260911P00300000 filled while queued
        AAPL260911P00295000 + two stale AAPL entries kept reserving $87k).
        Each dropped entry's prefunded dollars are consumed by the fill,
        same as an exact match.
        """
        hit = [e for e in self.entries
               if e.get("symbol") == symbol
               or (underlying and e.get("underlying") == underlying)]
        if not hit:
            return False
        for e in hit:
            self.prefunded = max(0.0, self.prefunded - float(e.get("need", 0) or 0))
        dropped_extra = [e for e in hit if e.get("symbol") != symbol]
        self.entries = [e for e in self.entries
                        if not (e.get("symbol") == symbol
                                or (underlying and e.get("underlying") == underlying))]
        self.dirty = True
        logger.info(f"[FUND QUEUE] {symbol} funded and filled - removed from queue")
        for e in dropped_extra:
            logger.info(f"[FUND QUEUE] also dropped {e.get('symbol')} (${float(e.get('need', 0)):.0f}) - same underlying {underlying} now has an open CSP")
        return True

    # ---- funding math ----
    def pending_need(self) -> float:
        return sum(float(e.get("need", 0) or 0) for e in self.entries)

    def pending_need_except(self, underlying: str) -> float:
        """Pending need ignoring entries for one underlying.

        add() REPLACES any same-underlying entry, so when the headroom cap
        weighs a fresh candidate it must not also count the stale entry it
        is about to replace — that double-counts one contract's risk as two
        (2026-08-21: fresh BAC $59 CSP skipped because the cap added stale
        queued BAC $57.50 + fresh $59 = $11.65k against $6.75k headroom;
        the replacement never happened and the stale entry expired unsold).
        """
        return sum(float(e.get("need", 0) or 0)
                   for e in self.entries
                   if e.get("underlying") != underlying)

    def reserve_amount(self, opt_bp: float | None) -> float:
        """Cash that must stay OUT of the SGOV sweep for queued candidates.

        Settled-cash options BP already covers that portion, so only the
        excess needs earmarking.
        """
        return max(0.0, self.pending_need() - max(opt_bp or 0.0, 0.0))

    def prefund_deficit(self, opt_bp: float | None) -> float:
        """Dollars of NEW SGOV sale still needed for the queue this run.

        Subtracts settled-cash BP and what earlier runs already sold, so a
        same-day second run never double-sells for the same entries.
        """
        return max(0.0, self.pending_need() - max(opt_bp or 0.0, 0.0) - self.prefunded)

    def record_prefund(self, amount: float, qty: int = 0) -> None:
        self.prefunded += max(0.0, amount)
        if qty > 0:
            self.last_prefund = {"qty": int(qty),
                                 "at": datetime.now().astimezone().isoformat(timespec="seconds")}
        self.dirty = True

    def pending_prefund_qty(self, max_age_min: float = 15) -> int:
        """Shares from a pre-fund sale submitted moments ago.

        The sweep's open-orders guard can't see a market order that already
        filled, and Alpaca's position endpoint lags the fill (2026-08-21
        morning run: pre-fund sold 10 at 10:07:09 ET; the sweep 29s later
        still read the pre-sale qty and sold 10 MORE - a same-run
        double-sell the Aug 18 guard was meant to prevent). Recording the
        qty here lets the sweep subtract it even after the broker's order/
        position views have moved on. Self-expires: after max_age_min the
        sale is either reflected in positions or was rejected, and either
        way it must no longer suppress the sweep.
        """
        lp = self.last_prefund or {}
        at_raw = str(lp.get("at", "") or "")
        qty = int(lp.get("qty", 0) or 0)
        if not at_raw or qty <= 0:
            return 0
        try:
            at = datetime.fromisoformat(at_raw)
        except ValueError as e:
            logger.debug("[SWALLOWED] funding queue last_prefund has bad timestamp %r, ignoring it: %r", at_raw, e)
            return 0
        age = (datetime.now().astimezone() - at).total_seconds()
        return qty if 0 <= age <= max_age_min * 60 else 0
