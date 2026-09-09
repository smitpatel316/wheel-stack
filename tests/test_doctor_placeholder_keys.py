"""doctor placeholder-key reporting (P4 audit).

Regression: config/credentials.py ships placeholder defaults for the data
keys (FINNHUB_API_KEY="***REMOVED***3g***REMOVED***40",
ALPHA_VANTAGE_API_KEY="***REMOVED***"). ./wheel doctor's check_config treated
any truthy value as configured and reported PASS "<key> present" -- so a
fresh clone with no real keys looked pre-flight green on the data-feed keys,
and check_data_sources burned a real network ping with a garbage key.

A placeholder is not a key: doctor must report it as not configured (WARN),
never as present (PASS).
"""
import importlib.util
import os
import sys
from unittest import mock

import pytest

SPEC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "doctor.py",
)


def load_doctor():
    spec = importlib.util.spec_from_file_location("wheel_doctor", SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wheel_doctor"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def doctor_mod():
    mod = load_doctor()
    mod._results.clear()
    yield mod
    mod._results.clear()


def _patch_cred(monkeypatch, **overrides):
    import config.credentials as cred
    base = {
        "ALPACA_API_KEY": "PKDUMMYKEY",
        "ALPACA_SECRET_KEY": "dummysecret",
        "IS_PAPER": True,
        "FINNHUB_API_KEY": "***REMOVED***3g***REMOVED***40",
        "ALPHA_VANTAGE_API_KEY": "***REMOVED***",
    }
    base.update(overrides)
    for k, v in base.items():
        monkeypatch.setattr(cred, k, v)


def _statuses(mod, check):
    return [(s, m) for (s, c, m) in mod._results if c == check]


def test_placeholder_keys_not_reported_present(doctor_mod, monkeypatch):
    _patch_cred(monkeypatch)
    doctor_mod.check_config()
    msgs = " ".join(m for _, m in _statuses(doctor_mod, "config"))
    assert "FINNHUB_API_KEY present" not in msgs
    assert "ALPHA_VANTAGE_API_KEY present" not in msgs
    warns = [m for s, m in _statuses(doctor_mod, "config") if s == "WARN"]
    assert any("FINNHUB_API_KEY" in m for m in warns)
    assert any("ALPHA_VANTAGE_API_KEY" in m for m in warns)


def test_real_keys_still_reported_present(doctor_mod, monkeypatch):
    _patch_cred(monkeypatch, FINNHUB_API_KEY="realfinnhubkey",
                ALPHA_VANTAGE_API_KEY="realalphakey")
    doctor_mod.check_config()
    passes = [m for s, m in _statuses(doctor_mod, "config") if s == "PASS"]
    assert any("FINNHUB_API_KEY present" in m for m in passes)
    assert any("ALPHA_VANTAGE_API_KEY present" in m for m in passes)


def test_placeholder_keys_skip_network_ping(doctor_mod, monkeypatch):
    # With placeholder keys the data-source check must not attempt a real
    # HTTP ping with the garbage key -- it reports not-configured instead.
    _patch_cred(monkeypatch)
    with mock.patch("requests.get") as mock_get:
        doctor_mod.check_data_sources()
    mock_get.assert_not_called()
    warns = [m for s, m in _statuses(doctor_mod, "data") if s == "WARN"]
    assert any("Finnhub" in m and "not configured" in m for m in warns)
    assert any("Alpha Vantage" in m and "not configured" in m for m in warns)
