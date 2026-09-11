"""postrun_verify reader robustness.

Regression (2026-09-10, reporting audit): the verifier is a reporting
tool - it must always produce a verdict, never die with a traceback.

1. optionable-sync detail extraction crashed with AttributeError when the
   log mentioned "not reachable"/"held in local outbox" but had no
   "[SYNC] ..." line (the engine's current "Optionable not reachable -
   N payload(s) held in local outbox" line carries no [SYNC] prefix).
2. A missing/unreadable log path died with a raw FileNotFoundError
   traceback instead of a FAIL verdict.
"""
import importlib.util
import os
import sys

import pytest

SPEC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "postrun_verify.py",
)


def load_mod(name):
    spec = importlib.util.spec_from_file_location(name, SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(pv, log_path):
    pv._fetch_optionable_health = lambda: (False, "unreachable")
    sys.argv = ["postrun_verify.py", str(log_path)]
    try:
        pv.main()
    except SystemExit as e:
        return e.code
    return 0


def test_plain_not_reachable_line_no_crash(monkeypatch, tmp_path, capsys):
    """Engine's real format: no [SYNC] prefix on the not-reachable line."""
    pv = load_mod("postrun_verify_robust_plain")
    monkeypatch.delenv("SYNC_OUTBOX_DIR", raising=False)
    log = tmp_path / "run.log"
    log.write_text(
        "ENGINE-EXIT=0\n"
        "Optionable not reachable - 2 payload(s) held in local outbox, will retry next run\n"
        "[DASH] pushed snapshot+scan funnel (8 symbols, 2 positions, 10 equity pts) to Optionable\n"
    )
    code = _run(pv, log)
    out = capsys.readouterr().out
    assert "Traceback" not in capsys.readouterr().err
    assert "FAIL optionable-sync" in out
    assert "held in local outbox" in out
    assert code in (1, 2)


def test_held_in_outbox_without_sync_prefix_no_crash(monkeypatch, tmp_path, capsys):
    pv = load_mod("postrun_verify_robust_held")
    monkeypatch.delenv("SYNC_OUTBOX_DIR", raising=False)
    log = tmp_path / "run.log"
    log.write_text("ENGINE-EXIT=0\ntrades held in local outbox pending retry\n")
    code = _run(pv, log)
    out = capsys.readouterr().out
    assert "Traceback" not in capsys.readouterr().err
    assert "FAIL optionable-sync" in out
    assert code in (1, 2)


def test_missing_log_file_produces_fail_verdict(tmp_path, capsys):
    pv = load_mod("postrun_verify_robust_missing")
    code = _run(pv, tmp_path / "does-not-exist.log")
    out = capsys.readouterr().out
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "VERDICT: FAIL" in out
    assert "cannot read" in out
    assert code == 1
