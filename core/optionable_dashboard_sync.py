"""
Optionable dashboard push — engine observability (capital card + scan funnel).

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
import logging
import os
import re
import sys
from typing import List, Optional

import requests

logger = logging.getLogger("strategy.optionable_dashboard_sync")

OPTIONABLE_URL = os.getenv("OPTIONABLE_URL", "http://localhost:8096")
TIMEOUT = 8


class _TeeStream:
    """Passes writes through to the real stream and mirrors lines to the collector."""

    def __init__(self, real, on_line):
        self._real = real
        self._on_line = on_line
        self._buf = ""

    def write(self, s):
        try:
            self._real.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            try:
                self._on_line(line)
            except Exception:
                pass

    def flush(self):
        try:
            self._real.flush()
        except Exception:
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
        """Tee stdout/stderr so printed and logged lines are both parsed."""
        try:
            if self._orig_stdout is None:
                self._orig_stdout, self._orig_stderr = sys.stdout, sys.stderr
                sys.stdout = _TeeStream(self._orig_stdout, self._on_line)
                sys.stderr = _TeeStream(self._orig_stderr, self._on_line)
        except Exception as e:
            logger.warning(f"[DASH] install failed: {e}")

    def uninstall(self):
        try:
            if self._orig_stdout is not None:
                sys.stdout, sys.stderr = self._orig_stdout, self._orig_stderr
                self._orig_stdout = self._orig_stderr = None
        except Exception:
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
            except Exception:
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
            if not payload:
                logger.info("[DASH] nothing collected this run - skipping push")
                return False
            r = requests.post(f"{self.base_url}/api/engine/dashboard", json=payload, timeout=TIMEOUT)
            if r.status_code == 200:
                logger.info(f"[DASH] pushed snapshot+scan funnel ({len(rows)} symbols) to Optionable")
                return True
            logger.warning(f"[DASH] Optionable push HTTP {r.status_code}: {r.text[:200]}")
            return False
        except Exception as e:
            logger.warning(f"[DASH] push failed (non-fatal): {e}")
            return False


def push_now(snapshot: dict = None, scan_run: dict = None, base_url: Optional[str] = None):
    """One-shot push for callers that already have structured data."""
    try:
        payload = {}
        if snapshot:
            payload["snapshot"] = snapshot
        if scan_run:
            payload["scanRun"] = scan_run
        if not payload:
            return False
        r = requests.post(f"{base_url or OPTIONABLE_URL}/api/engine/dashboard",
                          json=payload, timeout=TIMEOUT)
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"[DASH] push_now failed (non-fatal): {e}")
        return False
