"""postrun_verify must report real closer executions as actions (P4 audit).

Regression (archetype 3): core/closer.py logs a buy-to-close as
"[CLOSER] Close order <id> submitted for <sym>", but postrun_verify's
ACTION_KEEP only knew the formats "\\border submitted\\b" (needs the literal
"order submitted" with nothing between) and "\\bCLOSED at\\b" (a format the
engine never logs). A run that closed a position therefore reported
"ACTIONS: none (no orders/rolls/closes)" -- the exact misreport class from
the 2026-08-31 incident.
"""
import importlib.util
import io
import os
import sys

import pytest

SPEC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "postrun_verify.py",
)


def load_pv():
    spec = importlib.util.spec_from_file_location("postrun_verify_closer", SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["postrun_verify_closer"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pv():
    return load_pv()


REAL_CLOSER_SUBMIT = (
    "[2026-09-09 10:05:03] INFO [CLOSER] Close order "
    "2de702fc-4b1a-4e2a-9c1d-000000000001 submitted for "
    "JNJ260918P00260000 gross $121.00 net $120.35"
)
CLOSER_INTENT = (
    "[2026-09-09 10:05:01] INFO [CLOSER] Closing JNJ260918P00260000 "
    "profit 52% gross $121.00 net $120.35 fees $0.65 entry $2.80 cur $1.59 "
    "DTE 12 qty 1 delta -0.25 OTM 4.2%"
)
CLOSER_SYNC_NOTE = (
    "[2026-09-09 10:05:04] INFO [CLOSER] Will sync Optionable closePrice "
    "via Alpaca fill for JNJ260918P00260000 - avoids $0 phantom bug"
)


def test_closer_order_submission_is_an_action(pv):
    out = pv.extract_actions([REAL_CLOSER_SUBMIT])
    assert len(out) == 1
    assert "Close order" in out[0]
    assert "JNJ260918P00260000" in out[0]


def test_closer_intent_and_sync_note_are_not_actions(pv):
    # Pre-order intent and the post-close sync note are chatter, not
    # transactional evidence (a failed order must not phantom-report).
    assert pv.extract_actions([CLOSER_INTENT]) == []
    assert pv.extract_actions([CLOSER_SYNC_NOTE]) == []


def test_run_with_close_reports_actions_not_none(pv, tmp_path, monkeypatch, capsys):
    log = tmp_path / "run.log"
    log.write_text(
        "ENGINE-EXIT=0\n"
        f"{CLOSER_INTENT}\n"
        f"{REAL_CLOSER_SUBMIT}\n"
        f"{CLOSER_SYNC_NOTE}\n"
        "Synced positions to Optionable tracker (3 Alpaca positions)\n"
        "[DASH] pushed snapshot slot=run ok\n"
    )
    monkeypatch.delenv("OPTIONABLE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["postrun_verify.py", str(log)])
    with pytest.raises(SystemExit) as exc:
        pv.main()
    assert exc.value.code == 0  # VERDICT: OK
    out = capsys.readouterr().out
    assert "VERDICT: OK" in out
    assert "ACTIONS: none" not in out
    assert "Close order" in out
