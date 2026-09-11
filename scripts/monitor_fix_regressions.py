#!/usr/bin/env python3
"""Post-run regression monitor for the 2026-09-11 swarm fixes.

Run ~45 min after each strategy run (morning/midday/afternoon). Checks the
run's log plus the order-intent ledger for regressions introduced by the 11
merged swarm fixes. Prints findings and exits 1 on problems; exits 0 SILENT
on a clean run (the cron only relays output when there is something to say).

Usage: monitor_fix_regressions.py <morning|midday|afternoon>
"""
import glob
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(REPO, "logs")
LEDGER_DB = os.environ.get("ORDER_INTENT_DB") or os.path.join(REPO, "state", "order_intents.db")

_log = logging.getLogger("fix_monitor")


def latest_log(slot: str):
    today = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d")
    cands = sorted(glob.glob(os.path.join(LOG_DIR, f"cron-{slot}-{today}-*.log")))
    return cands[-1] if cands else None


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("morning", "midday", "afternoon"):
        print("usage: monitor_fix_regressions.py <morning|midday|afternoon>", file=sys.stderr)
        return 2
    slot = sys.argv[1]
    findings = []

    log = latest_log(slot)
    if not log:
        findings.append(f"no cron-{slot} log found for today")
        text = ""
    else:
        with open(log, errors="replace") as f:
            text = f.read()

        # 1. Hard failures in the run
        tb = re.findall(r"^Traceback \(most recent call last\):", text, re.M)
        if tb:
            findings.append(f"{len(tb)} traceback(s) in {os.path.basename(log)}")
        if "VERDICT: FAIL" in text:
            findings.append("postrun_verify reported VERDICT: FAIL")

        # 2. The new broker guards must not misfire in Alpaca paper mode.
        # A "Robinhood mode" skip in an Alpaca run = false-positive guard.
        if "BROKER=robinhood" not in text and "broker=robinhood" not in text.lower():
            if "Robinhood mode: skipping engine dashboard push" in text:
                findings.append("DASH GUARD MISFIRE: RH dashboard-push skip fired in a non-RH run")
            if re.search(r"\[DRY-RUN\].*not pushed to Optionable", text) and "RH_DRY_RUN" not in text:
                # dry-run guard firing outside dry-run would hide real pushes
                pass  # the log line itself only prints in dry-run; informational

        # 3. Ledger-side anomalies from the RH adapter fixes
        if "RHOrderFilledError" in text:
            findings.append("RHOrderFilledError raised during run - verify it was a genuine race fill")
        if "NEEDS_REVIEW" in text:
            findings.append("ledger NEEDS_REVIEW intent seen - fail-closed path engaged, needs eyes")

        # 4. The reporting fixes must not error
        if "reconcile_pnl" in text and re.search(r"reconcile_pnl.*(Error|error|Traceback)", text):
            findings.append("reconcile_pnl error in run output")

    # 5. Ledger DB: intents stuck in non-terminal states from recent runs
    if os.path.exists(LEDGER_DB):
        try:
            conn = sqlite3.connect(LEDGER_DB)
            conn.row_factory = sqlite3.Row
            stuck = conn.execute(
                "SELECT state, COUNT(*) c FROM intents "
                "WHERE state IN ('SENDING','PLACED_UNCONFIRMED','UNKNOWN','NEEDS_REVIEW') "
                "GROUP BY state").fetchall()
            conn.close()
            for row in stuck:
                findings.append(f"ledger has {row['c']} intent(s) stuck in {row['state']}")
        except Exception as e:
            _log.warning("[SWALLOWED] could not read order-intent ledger: %r", e)
            findings.append(f"could not read order-intent ledger: {e!r}")

    if findings:
        print(f"[FIX-MONITOR] {slot} run: {len(findings)} finding(s)")
        for f in findings:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
