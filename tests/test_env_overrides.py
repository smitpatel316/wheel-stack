"""Env-override layer (config/params.py v2.8): env must win over file defaults,
and removing the env var must restore the file default on reload."""
import importlib
import os

import pytest

import config.params as params


@pytest.fixture
def fresh_params(monkeypatch):
    """Reload config.params with chosen env, restore pristine module after."""
    for k in ("MAX_RISK", "MIN_PREMIUM", "SCORE_MIN", "SGOV_ENABLED", "WATCHLIST",
              "DELTA_MAX", "EARNINGS_ENABLED", "SGOV_CASH_BUFFER"):
        monkeypatch.delenv(k, raising=False)
    yield monkeypatch
    for k in ("MAX_RISK", "MIN_PREMIUM", "SCORE_MIN", "SGOV_ENABLED", "WATCHLIST",
              "DELTA_MAX", "EARNINGS_ENABLED", "SGOV_CASH_BUFFER"):
        os.environ.pop(k, None)
    importlib.reload(params)


def test_env_wins_over_defaults(fresh_params):
    fresh_params.setenv("MAX_RISK", "1000")
    fresh_params.setenv("MIN_PREMIUM", "0.10")
    fresh_params.setenv("SCORE_MIN", "0.01")
    fresh_params.setenv("SGOV_ENABLED", "true")
    fresh_params.setenv("DELTA_MAX", "0.25")
    p = importlib.reload(params)
    assert p.MAX_RISK == 1000.0
    assert p.MIN_PREMIUM == 0.10
    assert p.SCORE_MIN == 0.01
    assert p.SGOV_ENABLED is True
    assert p.DELTA_MAX == 0.25


def test_defaults_restored_without_env(fresh_params):
    p = importlib.reload(params)
    assert p.MAX_RISK == 90_000
    assert p.MIN_PREMIUM == 0.20
    assert p.SCORE_MIN == 0.02
    # 2026-08-28: default flipped back to True for the v2.8 float-model live
    # paper test (Smit reversed the 2026-08-27 clean-week flag-off).
    assert p.SGOV_ENABLED is True


def test_bool_parsing_variants(fresh_params):
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        fresh_params.setenv("SGOV_ENABLED", truthy)
        assert importlib.reload(params).SGOV_ENABLED is True
    for falsy in ("0", "false", "no", "off", "nonsense"):
        fresh_params.setenv("SGOV_ENABLED", falsy)
        assert importlib.reload(params).SGOV_ENABLED is False


def test_invalid_number_keeps_default(fresh_params):
    fresh_params.setenv("MAX_RISK", "not-a-number")
    assert importlib.reload(params).MAX_RISK == 90_000


def test_watchlist_env_override(fresh_params):
    fresh_params.setenv("WATCHLIST", "f, spy ,F")
    p = importlib.reload(params)
    assert p.load_watchlist() == ["F", "SPY"]  # uppercased, deduped not required
    assert p.watchlist_source() == "env:WATCHLIST"


def test_watchlist_file_default(fresh_params):
    p = importlib.reload(params)
    wl = p.load_watchlist()
    assert len(wl) == 25 and "F" in wl and "SPY" in wl
    assert p.watchlist_source() == "config/symbol_list.txt"


def test_watchlist_blank_env_falls_back_to_file(fresh_params):
    fresh_params.setenv("WATCHLIST", "   ")
    p = importlib.reload(params)
    assert len(p.load_watchlist()) == 25
