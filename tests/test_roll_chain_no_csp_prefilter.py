"""Regression guard: roll-target chains must not pass through the new-CSP screen.

2026-08-26: NEE $82.5P was 2% OTM with a medium-urgency roll signal, but
run_strategy passed the roll chain through filter_options (the new-entry
CSP screen) first; it emptied the chain and the roll was never evaluated.
find_roll_targets applies its own roll-specific filters on the raw chain.
"""
import pathlib


def test_roll_targets_use_raw_chain_not_csp_prefilter():
    src = pathlib.Path("scripts/run_strategy.py").read_text()
    assert "filter_options(avail" not in src, (
        "roll chain re-entered the new-CSP pre-filter - rolls must be "
        "evaluated against the raw chain (find_roll_targets filters itself)"
    )
    assert "find_roll_targets(decision.candidate, avail," in src
