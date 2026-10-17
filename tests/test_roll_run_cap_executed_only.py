"""Regression: per-run roll cap counts EXECUTED rolls, not attempts.

Bug (root-caused by the 2026-10-12 QA swarm, quantified Sep 28-Oct 12):
scripts/run_strategy.py sliced the roll-candidate list with capped[:2]
BEFORE calling find_roll_targets(). The per-run budget of 2 was therefore
spent on the first two lineages in sort order whether or not a roll was
even possible for them. BAC and T - deep ITM for weeks with zero
qualifying targets in every scan - burned 60 of 64 slots in the window;
only 4 rolls executed. Positions queued behind them (PLD starved 5 runs;
INTC flagged high-urgency defensive on 2026-10-16 with 35 DTE) were never
evaluated at all.

Fix (2026-10-16, Smit's go): every candidate is evaluated in order; only
a successful roll_position() increments rolls_executed, and evaluation
stops once MAX_ROLLS_PER_RUN rolls have executed.

Companion policy (same change): rolls are credit-only in all cases - the
v2.5 debit override (min_credit -$0.20 for critical DTE<=1) is removed
from both run_strategy's config and find_roll_targets(). Behavioural
coverage for the credit floor lives in
tests/stress/test_closer_roller.py::test_ro10_no_debit_even_when_critical.
"""
from pathlib import Path

from core.state_manager import MAX_ROLLS_PER_RUN

REPO = Path(__file__).resolve().parents[1]


def test_run_cap_constant_is_two():
    assert MAX_ROLLS_PER_RUN == 2


def test_run_strategy_no_longer_slices_candidates_before_evaluation():
    src = (REPO / "scripts" / "run_strategy.py").read_text()
    assert "capped[:2]" not in src
    # The executed-counter is the mechanism: present and incremented only
    # in the success branch.
    assert "rolls_executed = 0" in src
    assert "rolls_executed >= MAX_ROLLS_PER_RUN" in src
    assert "rolls_executed += 1" in src


def test_no_debit_roll_paths_remain():
    run_src = (REPO / "scripts" / "run_strategy.py").read_text()
    roller_src = (REPO / "core" / "roller.py").read_text()
    # The -$0.20 debit allowance must not survive in either layer.
    assert "min_credit = -0.20" not in roller_src
    assert "-0.20 if" not in run_src
    assert "DEBIT AVOID ASSIGNMENT" not in run_src
    assert "attempt debit roll" not in run_src
