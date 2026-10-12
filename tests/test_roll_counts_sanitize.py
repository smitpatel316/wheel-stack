"""Regression (swarm/roller-logic): valid JSON with the wrong shape must not
bypass roll-count corrupt-file handling.

Before: load_roll_counts() returned whatever json.load() produced. A
top-level list crashed prune_roll_counts() ('list' object has no attribute
'items') inside the roll phase, killing ALL rolls for that run; a string
count crashed the `n >= MAX_ROLLS_PER_LINEAGE` comparison the same way; a
negative count silently weakened the lineage cap (a -1 count needs 3
executed rolls to reach the cap of 2). A hand-edit, a torn write from a
non-atomic writer, or another tool touching state/roll_counts.json could
produce any of these.

Contract: non-object JSON is quarantined like corrupt JSON (backup +
ERROR + {}); malformed lineage keys are dropped loudly; malformed or
out-of-range counts clamp to MAX_ROLLS_PER_LINEAGE (fail-closed).
"""
import glob
import json
import logging

from core.state_manager import (MAX_ROLLS_PER_LINEAGE, load_roll_counts,
                                prune_roll_counts, save_roll_counts)


def test_non_dict_json_quarantined_like_corrupt(tmp_path, caplog):
    p = tmp_path / "roll_counts.json"
    p.write_text('["BAC:P", 2]')

    with caplog.at_level(logging.ERROR, logger="core.state_manager"):
        counts = load_roll_counts(path=str(p))

    assert counts == {}
    assert len(glob.glob(str(tmp_path / "roll_counts.json.corrupt-*"))) == 1
    assert not p.exists()
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_string_count_clamped_fail_closed(tmp_path):
    p = tmp_path / "roll_counts.json"
    p.write_text('{"BAC:P": "2"}')
    counts = load_roll_counts(path=str(p))
    assert counts == {"BAC:P": MAX_ROLLS_PER_LINEAGE}
    # and the cap comparison downstream no longer crashes
    assert counts["BAC:P"] >= MAX_ROLLS_PER_LINEAGE


def test_negative_and_over_cap_counts_clamped(tmp_path):
    p = tmp_path / "roll_counts.json"
    p.write_text('{"BAC:P": -1, "T:P": 7, "PLD:P": 1}')
    counts = load_roll_counts(path=str(p))
    assert counts["BAC:P"] == MAX_ROLLS_PER_LINEAGE  # -1 must not weaken the cap
    assert counts["T:P"] == MAX_ROLLS_PER_LINEAGE
    assert counts["PLD:P"] == 1  # valid entry untouched


def test_malformed_keys_dropped_valid_kept(tmp_path):
    p = tmp_path / "roll_counts.json"
    p.write_text('{"BAC:P": 1, "garbage": 1, "T:X": 2, "MP:C": 0}')
    counts = load_roll_counts(path=str(p))
    assert counts == {"BAC:P": 1, "MP:C": 0}
    # prune (the old crash site) works on the sanitized result
    states = {"BAC": {"type": "short_put"}, "MP": {"type": "short_call"}}
    assert prune_roll_counts(counts, states) == counts


def test_save_leaves_no_per_pid_tmp_behind(tmp_path):
    p = tmp_path / "roll_counts.json"
    save_roll_counts({"BAC:P": 1}, path=str(p))
    assert json.loads(p.read_text()) == {"BAC:P": 1}
    assert glob.glob(str(tmp_path / "*.tmp*")) == []
