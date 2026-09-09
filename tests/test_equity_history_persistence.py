"""Regression tests: equity/sgov history persistence (P3 audit, 2026-09-09).

Incident class: on 2026-09-09 a non-serializable Alpaca UUID truncated
logs/strategy_log.json mid-write, and the next run's silent
"unreadable -> start fresh" handler wiped the whole history (~3.2MB).
Commit d045074 fixed strategy_log.json and market_context.json, but the
equity_history.json / sgov_history.json writers in scripts/run_strategy.py
kept the identical latent pattern:

  1. non-atomic write: eq_path.write_text(json.dumps(...)) — a crash
     mid-write leaves a truncated file;
  2. silent reset on corrupt read: "equity history load failed, restarting
     history from empty" — the next run then permanently discards weeks of
     equity/benchmark history by writing a fresh list over it.

These tests pin the fixed behavior: corrupt bytes are preserved as
.corrupt-<ts>UTC and surfaced at ERROR, writes are temp+rename atomic,
and non-serializable values stringify instead of killing the dump.
"""
import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.run_strategy import _append_history_atomic, _load_history_list


def test_corrupt_history_preserved_not_silently_wiped(tmp_path):
    """A truncated history file must be preserved for forensics, not
    discarded with a quiet warning (the 2026-09-09 strategy_log wipe)."""
    p = tmp_path / "equity_history.json"
    corrupt = '[{"t": "2026-09-01T10:00:00-04:00", "equity": 100000.0}, {"t": "2026-09-02T10:00'
    p.write_text(corrupt)

    hist = _load_history_list(p)

    assert hist == []
    backups = list(tmp_path.glob("equity_history.json.corrupt-*UTC"))
    assert len(backups) == 1, "corrupt bytes must be preserved, not deleted"
    assert backups[0].read_text() == corrupt
    assert not p.exists(), "truncated file must be moved aside, not left in place"


def test_append_after_corrupt_starts_fresh_but_keeps_backup(tmp_path):
    """After a corrupt read, the new write starts a fresh list — but the
    corrupt original must still exist as a backup."""
    p = tmp_path / "equity_history.json"
    p.write_text('{"t": "2026-09-01')  # truncated mid-object

    _append_history_atomic(p, {"t": "2026-09-09T10:00:00-04:00", "equity": 100500.0})

    assert json.loads(p.read_text()) == [
        {"t": "2026-09-09T10:00:00-04:00", "equity": 100500.0}
    ]
    assert len(list(tmp_path.glob("equity_history.json.corrupt-*UTC"))) == 1


def test_crash_mid_write_leaves_original_intact(tmp_path, monkeypatch):
    """Atomicity: a crash while writing must hit the temp file, never the
    live history. Simulates a kill between open/truncate and full flush."""
    p = tmp_path / "equity_history.json"
    before = [{"t": "2026-09-01T10:00:00-04:00", "equity": 100000.0}]
    p.write_text(json.dumps(before))

    real_write_text = Path.write_text

    def partial_write(self, data, *a, **k):
        if self.name.endswith(".tmp"):
            real_write_text(self, data[:10])  # partial bytes, then die
            raise RuntimeError("simulated crash mid-write")
        return real_write_text(self, data, *a, **k)

    monkeypatch.setattr(Path, "write_text", partial_write)
    with pytest.raises(RuntimeError, match="simulated crash mid-write"):
        _append_history_atomic(p, {"t": "2026-09-09T10:00:00-04:00", "equity": 100500.0})

    assert json.loads(p.read_text()) == before, "live history must be untouched"


def test_serialization_failure_cannot_truncate_history(tmp_path, monkeypatch):
    """The payload is fully serialized before any file is opened: a dump
    error must leave the existing history byte-identical."""
    p = tmp_path / "equity_history.json"
    before = [{"t": "2026-09-01T10:00:00-04:00", "equity": 100000.0}]
    p.write_text(json.dumps(before))

    def boom(*a, **k):
        raise TypeError("serialization exploded mid-dump")

    monkeypatch.setattr(json, "dumps", boom)
    with pytest.raises(TypeError):
        _append_history_atomic(p, {"t": "x", "equity": 1.0})

    assert json.loads(p.read_text()) == before


def test_nonserializable_values_stringify_instead_of_killing_dump(tmp_path):
    """Decimal equity / UUID order ids (the 2026-09-09 strategy_log killer)
    must stringify via default=str, never abort the write."""
    p = tmp_path / "equity_history.json"
    _append_history_atomic(
        p,
        {"t": "2026-09-09T10:00:00-04:00", "equity": Decimal("100193.88"),
         "order_id": uuid.uuid4()},
    )
    data = json.loads(p.read_text())
    assert len(data) == 1
    assert data[0]["equity"] == "100193.88"
    assert isinstance(data[0]["order_id"], str)


def test_missing_file_appends_cleanly(tmp_path):
    p = tmp_path / "sgov_history.json"
    _append_history_atomic(p, {"t": "2026-09-09T10:00:00-04:00", "shares": 10.0, "avg": 100.49})
    assert json.loads(p.read_text()) == [
        {"t": "2026-09-09T10:00:00-04:00", "shares": 10.0, "avg": 100.49}
    ]
    assert list(tmp_path.glob("*.tmp")) == [], "no temp files may linger"


def test_history_capped_at_max_entries(tmp_path):
    p = tmp_path / "equity_history.json"
    for i in range(10):
        _append_history_atomic(p, {"t": f"2026-09-09T10:0{i}:00-04:00", "equity": float(i)},
                               max_entries=5)
    data = json.loads(p.read_text())
    assert len(data) == 5
    assert data[0]["equity"] == 5.0
