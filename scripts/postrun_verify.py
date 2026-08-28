#!/usr/bin/env python3
"""Post-run verifier for the wheel strategy (Pi-migration era, 2026-08-28).

Replaces the old "review the log tail" eyeballing with a deterministic checklist.

Usage: python scripts/postrun_verify.py <run-log>

Reads the log file, then prints ONE verdict block the reporting agent relays:
  VERDICT: OK | DEGRADED | FAIL
  plus one line per check (name: detail), plus extracted trade/action lines.

Contract:
  OK       engine completed, all Pi integrations healthy, outbox empty
  DEGRADED engine completed but a Pi integration failed open (that is by
           design a valid run) — outbox may hold payloads for the next run
  FAIL     engine did not complete, traceback, or contract violated
"""
import json
import logging
import os
import re
import sys
import time
import urllib.request

logging.basicConfig(stream=sys.stderr, format="%(message)s")
logger = logging.getLogger("postrun_verify")


# --- Action extraction ------------------------------------------------------
# Keep ONLY transactional evidence: real order submissions/fills, executed
# rolls, closes, expiries, funding-queue additions. Informational scan/roller
# chatter must never count as an action.
#
# History (2026-08-27): a loose regex matched 'assignment' inside
# "assignment avoidance override" and 'SWEEP' inside "Sweep disabled",
# making clean no-op runs report phantom actions.
ACTION_KEEP = re.compile(
    r"(?:"
    r"\[ROLLER\] Rolling [A-Z0-9]{6,22} ->"      # executed roll
    r"|\[ROLL\] (?:Opening|Closing|Open order|Close order)"  # roll legs + fills
    r"|\border submitted\b"
    r"|\borderstatus\.filled\b"
    r"|\bFILLED\b"
    r"|\bExpired\b|\bworthless\b"
    r"|\bCLOSED at\b"
    r"|\bqueued for next\b"
    r"|\[ASSIGNMENT\]|\bASSIGNED\b"
    r")"
)
ACTION_SKIP = re.compile(
    r"Evaluating rolling need|need rolling|ranking|assignment avoidance|"
    r"Sweep disabled|\[SGOV\]|roll targets|roll cap|deadline pattern|"
    r"per-run|pre-fund|entry reconciliation|\bEvaluating\b|no-op"
)


def extract_actions(lines):
    """Return reportable action lines, filtering informational chatter."""
    out = []
    for ln in lines:
        if "[SWALLOWED]" in ln:
            continue
        if not ACTION_KEEP.search(ln):
            continue
        if ACTION_SKIP.search(ln):
            continue
        out.append(ln.strip()[:180])
    return out


def _fetch_optionable_health():
    """Pi Optionable /api/health with short retries.

    A single transient drop (RemoteDisconnected under load) should not flip
    an otherwise-good run to DEGRADED; 3 attempts 2s apart absorb that.
    """
    base = os.environ.get("OPTIONABLE_URL", "").rstrip("/")
    if not base:
        return True, "OPTIONABLE_URL unset, skipped"
    last_err = "unknown"
    for attempt in (1, 2, 3):
        try:
            req = urllib.request.Request(base + "/api/health", headers={"User-Agent": "curl/8.5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.load(r).get("data", {})
            return d.get("status") == "healthy", \
                f"healthy v{d.get('version')} {d.get('database', {}).get('tradeCount')} trades"
        except Exception as e:
            last_err = f"{type(e).__name__} (attempt {attempt}/3)"
            logger.debug("[SWALLOWED] optionable-api attempt %d: %s", attempt, type(e).__name__)
            if attempt < 3:
                time.sleep(2)
    logger.warning("[SWALLOWED] optionable-api check failed after retries: %s", last_err)
    return False, f"unreachable after 3 attempts: {last_err}"


def main():
    if len(sys.argv) < 2:
        print("usage: postrun_verify.py <run-log>", file=sys.stderr)
        sys.exit(64)
    LOG = sys.argv[1]
    text = open(LOG, errors="replace").read()
    lines = text.splitlines()

    checks = []

    def check(name, ok, detail, fatal=False):
        checks.append((name, ok, detail, fatal))

    # 1. Engine completion marker (cron wrapper appends ENGINE-EXIT=)
    m = re.search(r"^ENGINE-EXIT=(\d+)", text, re.M)
    check("engine-exit", m is not None and m.group(1) == "0",
          m.group(0) if m else "marker missing - run may have died mid-flight", fatal=True)

    # 2. No unhandled traceback
    tb = "Traceback (most recent call last)" in text
    check("no-traceback", not tb, "clean" if not tb else "TRACEBACK present", fatal=True)

    # 3. Earnings source (Pi) — current/applied/warned are all valid run states
    m = re.search(r"\[EARNINGS-SOURCE\] (.*)", text)
    if not m:
        check("earnings-source", True, "no line (unset or older code path)")
    elif "WARNING" in m.group(0) or "fail-open" in m.group(1) or "returned HTTP" in m.group(1) or "failed" in m.group(1).lower():
        check("earnings-source", False, f"fail-open used: {m.group(1)[:140]}")
    else:
        check("earnings-source", True, m.group(1)[:120])

    # 4. Optionable (Pi) position sync: direct ok vs held in outbox
    if re.search(r"Synced positions to Optionable tracker", text):
        check("optionable-sync", True, re.search(r"Synced positions to Optionable tracker \(([^)]*)\)", text).group(1))
    elif re.search(r"held in local outbox|not reachable", text, re.I):
        check("optionable-sync", False,
              (re.search(r"\[SYNC\] (.*)", text) or re.search(r"\[(.*?not reachable.*?)\]", text)).group(0)[:140])
    else:
        check("optionable-sync", False, "no sync evidence found in log")

    # 5. Dashboard push
    m = re.search(r"\[DASH\] pushed snapshot[^\n]*", text)
    if m:
        check("dash-push", True, m.group(0)[:120])
    elif re.search(r"\[DASH\].*(fail|HTTP|unreach)", text, re.I):
        check("dash-push", False, re.search(r"\[DASH\][^\n]*", text).group(0)[:140])
    else:
        check("dash-push", False, "no [DASH] evidence in log")

    # 6. Outbox drained empty by run end (BASE dir is wheel-stack)
    outbox = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "state", "sync-outbox")
    pending = len([f for f in os.listdir(outbox) if f.endswith(".json")]) if os.path.isdir(outbox) else 0
    check("outbox-empty", pending == 0, f"{pending} payload(s) retained for next run"
          if pending else "empty")

    # 7. Optionable (Pi) API sanity — transient drops get retried
    ok, detail = _fetch_optionable_health()
    check("optionable-api", ok, detail)

    actions = extract_actions(lines)

    fails = [c for c in checks if not c[1]]
    verdict = "FAIL" if any(c[3] for c in fails) else ("DEGRADED" if fails else "OK")
    print(f"VERDICT: {verdict}")
    for name, ok, detail, fatal in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}: {detail}")
    if actions:
        print("ACTIONS:")
        for a in actions[:12]:
            print(f"  - {a}")
    else:
        print("ACTIONS: none (no orders/rolls/closes)")
    sys.exit(0 if verdict == "OK" else (1 if verdict == "FAIL" else 2))


if __name__ == "__main__":
    main()
