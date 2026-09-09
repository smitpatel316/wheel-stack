"""Regression: state/roll_counts.json must survive crashes and corruption loudly.

Same incident class as the 2026-09-09 strategy_log.json wipe (fixed in
d045074): save_roll_counts() wrote the file in place, so a crash mid-write
leaves a truncated file; load_roll_counts() then swallowed the corrupt JSON
with a mere warning and returned {} - silently resetting the
MAX_ROLLS_PER_LINEAGE=2 risk cap. A position already rolled twice would be
treated as rolled zero times and could be rolled again, compounding losses
(the exact behavior the cap exists to prevent).

Fix contract (mirrors d045074): atomic tmp+rename saves, and corrupt files
are backed up to .corrupt-<ts> with an ERROR log instead of being silently
discarded.
"""
import glob
import json
import logging
import os

import pytest

from core.state_manager import load_roll_counts, save_roll_counts


def test_corrupt_file_backed_up_and_logged_loudly(tmp_path, caplog):
    p = tmp_path / "roll_counts.json"
    p.write_text('{"BAC:P": 2, "F:P": tru')  # truncated mid-write

    with caplog.at_level(logging.ERROR, logger="core.state_manager"):
        counts = load_roll_counts(path=str(p))

    assert counts == {}
    backups = glob.glob(str(tmp_path / "roll_counts.json.corrupt-*"))
    assert len(backups) == 1, "corrupt bytes were not preserved for forensics"
    assert "truncated" in open(backups[0]).read() or "BAC" in open(backups[0]).read()
    assert not p.exists(), "corrupt file must be moved aside, not left in place"
    assert any(r.levelno >= logging.ERROR for r in caplog.records), \
        "silent reset of the roll cap must be logged at ERROR"


def test_save_is_atomic_crash_mid_replace_keeps_old_file(tmp_path, monkeypatch):
    """If the final rename dies (crash/power loss), the previous good file
    must be untouched - never a truncated roll_counts.json."""
    p = tmp_path / "roll_counts.json"
    save_roll_counts({"BAC:P": 2}, path=str(p))

    def _boom(*a, **k):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        save_roll_counts({"BAC:P": 2, "F:P": 1}, path=str(p))

    assert json.loads(p.read_text()) == {"BAC:P": 2}, \
        "failed save clobbered the previous roll counts"


def test_save_roundtrip_leaves_no_tmp_behind(tmp_path):
    p = tmp_path / "roll_counts.json"
    save_roll_counts({"BAC:P": 1}, path=str(p))
    assert load_roll_counts(path=str(p)) == {"BAC:P": 1}
    assert glob.glob(str(tmp_path / "*.tmp")) == []
