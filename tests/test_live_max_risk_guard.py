"""Live Alpaca must require an explicit, valid MAX_RISK (P4 audit).

Regression: the live+paper tripwire in run_strategy.py only refused
MAX_RISK >= 100000, but the default is 90000 -- and config.params._env_float
silently restores that default for missing/invalid input. So IS_PAPER=false
with a missing or typo'd MAX_RISK sailed through "live" with the
paper-scale cap. A live run must name its cap explicitly; the >= 100000
paper-params refusal is retained as a second layer.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import run_strategy


def _guard(monkeypatch, is_paper, raw):
    if raw is None:
        monkeypatch.delenv("MAX_RISK", raising=False)
    else:
        monkeypatch.setenv("MAX_RISK", raw)
    return run_strategy._live_max_risk_guard(is_paper)


@pytest.mark.parametrize("raw", [None, "", "   ", "abc", "1,000", "0", "-100"])
def test_live_refuses_missing_or_invalid_max_risk(monkeypatch, raw):
    with pytest.raises(SystemExit):
        _guard(monkeypatch, False, raw)


def test_live_refuses_paper_scale_cap(monkeypatch):
    with pytest.raises(SystemExit):
        _guard(monkeypatch, False, "100000")


@pytest.mark.parametrize("raw", ["1000", "90000", " 2500 "])
def test_live_accepts_explicit_valid_max_risk(monkeypatch, raw):
    _guard(monkeypatch, False, raw)  # must not raise


@pytest.mark.parametrize("raw", [None, "", "abc"])
def test_paper_unaffected_by_max_risk_env(monkeypatch, raw):
    _guard(monkeypatch, True, raw)  # must not raise
