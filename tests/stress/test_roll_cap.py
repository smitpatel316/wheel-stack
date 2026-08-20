"""v2.6: roll-count cap per position lineage + loss-trigger gating."""
import json

from core.state_manager import (MAX_ROLLS_PER_LINEAGE, load_roll_counts,
                                prune_roll_counts, save_roll_counts)


def test_roll_counts_roundtrip(tmp_path):
    p = tmp_path / "roll_counts.json"
    save_roll_counts({"BAC:P": 1}, path=str(p))
    assert load_roll_counts(path=str(p)) == {"BAC:P": 1}


def test_roll_counts_missing_file(tmp_path):
    assert load_roll_counts(path=str(tmp_path / "nope.json")) == {}


def test_roll_counts_corrupt_file_recovers(tmp_path):
    p = tmp_path / "roll_counts.json"
    p.write_text("{not json")
    assert load_roll_counts(path=str(p)) == {}


def test_prune_drops_dead_lineages():
    counts = {"BAC:P": 2, "MP:P": 1, "OLD:P": 1}
    states = {
        "BAC": {"type": "short_put"},
        "MP": {"type": "long_shares"},  # assigned -> put lineage over
    }
    pruned = prune_roll_counts(counts, states)
    assert pruned == {"BAC:P": 2}  # MP:P pruned at assignment, OLD:P gone


def test_cap_constant_is_two():
    assert MAX_ROLLS_PER_LINEAGE == 2
