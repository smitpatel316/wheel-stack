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

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _outbox_dir():
    """The engine's real outbox location (core.sync_outbox.outbox_dir).

    2026-09-10 (reporting audit): the old code hardcoded
    state/sync-outbox, but the engine honors SYNC_OUTBOX_DIR - so with the
    env set the verifier reported "outbox empty" while payloads sat in the
    real outbox (seen in logs/e2e-killpi-drill.log: optionable-sync FAIL
    "held in local outbox" alongside outbox-empty PASS). Ask the engine.
    """
    try:
        from core.sync_outbox import outbox_dir
        return str(outbox_dir())
    except Exception as e:
        logger.warning("[SWALLOWED] outbox_dir import failed, falling back to env/default: %r", e)
        env = os.environ.get("SYNC_OUTBOX_DIR", "").strip()
        return env or os.path.join(_REPO_ROOT, "state", "sync-outbox")


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
    r"|\[CLOSER\] Close order .* submitted for"  # executed buy-to-close (core/closer.py);
    # 2026-09-09 P4: the old "\border submitted\b" never matched this line
    # (the order id sits between "order" and "submitted"), so runs that
    # closed positions reported "ACTIONS: none".
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


def _optionable_base():
    """OPTIONABLE_URL from the environment, falling back to the repo .env.

    The cron wrapper sources .env before verify, but a verify call from a
    bare shell (midday run 2026-08-31) silently skipped this check — a
    coverage gap. Parse .env ourselves; only this one key is read and no
    file values are ever logged.
    """
    base = os.environ.get("OPTIONABLE_URL", "").strip()
    if not base:
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
        )
        try:
            with open(env_path, errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln.startswith("OPTIONABLE_URL="):
                        base = ln.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except OSError as e:
            logger.warning("[SWALLOWED] .env fallback read failed: %s", type(e).__name__)
    return base.rstrip("/")


def _fetch_optionable_health():
    """Pi Optionable /api/health with short retries.

    A single transient drop (RemoteDisconnected under load) should not flip
    an otherwise-good run to DEGRADED; 3 attempts 2s apart absorb that.
    """
    base = _optionable_base()
    if not base:
        return True, "OPTIONABLE_URL unset in env and .env, skipped"
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
    # 2026-09-10 (reporting audit): a wrong/missing log path used to die with
    # a raw FileNotFoundError traceback. A verifier must always produce a
    # verdict the reporting agent can relay.
    try:
        text = open(LOG, errors="replace").read()
    except OSError as e:
        logger.error("cannot read run log %s: %r", LOG, e)
        print("VERDICT: FAIL")
        print(f"  FAIL engine-log: cannot read {LOG}: {type(e).__name__}: {e}")
        sys.exit(1)
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
        # 2026-09-10 (reporting audit): the old one-liner called .group(0) on
        # the first non-None of two regexes, but BOTH can be None - e.g. the
        # engine's current "Optionable not reachable - N payload(s) held in
        # local outbox" line carries no [SYNC] prefix - so the verifier died
        # with AttributeError instead of printing a verdict. Degrade to the
        # plain-text line, never crash.
        detail_m = (re.search(r"\[SYNC\] (.*)", text)
                    or re.search(r"\[([^]\n]*?(?:not reachable|held in local outbox)[^]\n]*?)\]", text, re.I)
                    or re.search(r"^.*(?:not reachable|held in local outbox).*$", text, re.I | re.M))
        check("optionable-sync", False,
              (detail_m.group(0) if detail_m else "sync evidence found but detail line unparseable")[:140])
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

    # 6. Outbox drained empty by run end - use the ENGINE's outbox location
    # (core.sync_outbox.outbox_dir, honors SYNC_OUTBOX_DIR), not a hardcoded
    # path; otherwise this check can disagree with the optionable-sync check.
    outbox = _outbox_dir()
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
