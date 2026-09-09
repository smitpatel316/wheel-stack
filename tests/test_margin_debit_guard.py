"""Margin-debit guard must fail closed (2026-09-09).

Regression: scripts/run_strategy.py assigned ``_acct`` inside a ``try`` and
read it in a second ``try`` for the margin-debit pre-check. When
``get_account()`` raised, the second block hit ``NameError``, the
``except Exception`` swallowed it at debug level, and the run traded with no
margin check and no account data at all.

The guard is now ``_margin_debit_guard(acct)``: it raises SystemExit(2) on a
margin debit AND when the account is unreadable (None) — trading blind is
not an option.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import run_strategy


def test_unreadable_account_aborts():
    # get_account() failed upstream -> _acct is None -> the run must abort,
    # not continue to the order path with no margin check.
    with pytest.raises(SystemExit) as exc:
        run_strategy._margin_debit_guard(None)
    assert exc.value.code == 2


def test_margin_debit_aborts():
    with pytest.raises(SystemExit) as exc:
        run_strategy._margin_debit_guard(SimpleNamespace(cash=-12.50))
    assert exc.value.code == 2


def test_healthy_account_passes():
    assert run_strategy._margin_debit_guard(SimpleNamespace(cash=101340.55)) == 101340.55


def test_zero_cash_passes():
    assert run_strategy._margin_debit_guard(SimpleNamespace(cash=0)) == 0
