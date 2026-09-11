"""postrun_verify outbox-empty check uses the engine's outbox location.

Regression (2026-09-10, reporting audit): the check hardcoded
state/sync-outbox while core.sync_outbox.outbox_dir() honors
SYNC_OUTBOX_DIR. With the env set, the verifier reported
"PASS outbox-empty: empty" while payloads sat in the real outbox -
seen live in logs/e2e-killpi-drill.log (optionable-sync FAIL "held in
local outbox" alongside outbox-empty PASS). The verifier must ask the
engine where the outbox is.
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
    pv._fetch_optionable_health = lambda: (True, "healthy vX 0 trades")
    sys.argv = ["postrun_verify.py", str(log_path)]
    try:
        pv.main()
    except SystemExit as e:
        return e.code
    return 0


def _write_clean_log(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(
        "ENGINE-EXIT=0\n"
        "Synced positions to Optionable tracker (2 Alpaca positions)\n"
        "[DASH] pushed snapshot+scan funnel (8 symbols, 2 positions, 10 equity pts) to Optionable\n"
    )
    return log


def test_outbox_empty_uses_sync_outbox_dir_env(monkeypatch, tmp_path, capsys):
    pv = load_mod("postrun_verify_outbox_dir_env")
    outbox = tmp_path / "real-outbox"
    outbox.mkdir()
    (outbox / "1699999999999-abc123.json").write_text('{"id": "abc123", "kind": "trades"}')
    monkeypatch.setenv("SYNC_OUTBOX_DIR", str(outbox))
    log = _write_clean_log(tmp_path)
    code = _run(pv, log)
    out = capsys.readouterr().out
    assert code == 2, out  # DEGRADED, not OK
    assert "FAIL outbox-empty: 1 payload(s) retained for next run" in out


def test_outbox_empty_passes_when_env_outbox_empty(monkeypatch, tmp_path, capsys):
    pv = load_mod("postrun_verify_outbox_dir_env_empty")
    outbox = tmp_path / "real-outbox"
    outbox.mkdir()
    monkeypatch.setenv("SYNC_OUTBOX_DIR", str(outbox))
    log = _write_clean_log(tmp_path)
    code = _run(pv, log)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PASS outbox-empty: empty" in out


def test_outbox_empty_uses_default_repo_dir_when_env_unset(monkeypatch, tmp_path, capsys):
    """Without SYNC_OUTBOX_DIR the check must agree with the engine default."""
    pv = load_mod("postrun_verify_outbox_dir_default")
    monkeypatch.delenv("SYNC_OUTBOX_DIR", raising=False)
    log = _write_clean_log(tmp_path)
    code = _run(pv, log)
    out = capsys.readouterr().out
    # repo state/sync-outbox: empty in a clean checkout or has real pending
    # items; either way the verifier must report the SAME dir the engine uses.
    from core.sync_outbox import outbox_dir
    assert pv._outbox_dir() == str(outbox_dir())
    assert ("outbox-empty" in out)
