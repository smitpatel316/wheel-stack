"""Action-extraction + health-retry unit tests for postrun_verify.

Regression (2026-08-27): clean runs showed phantom ACTIONS because
'assignment avoidance override' matched /assignment/ and
'[SGOV] Sweep disabled...' matched /SWEEP/i.
"""
import importlib.util
import io
import json
import os
import sys
import types
import urllib.error
import urllib.request
from unittest import mock

import pytest

spec = importlib.util.spec_from_file_location(
    "postrun_verify",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "postrun_verify.py"),
)
pv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pv)


REAL_ACTION_LINES = [
    "[[ROLLER] Rolling INTC260918P00090000 -> INTC261016P00087500 net $1.21 ...]",
    "[[ROLL] Opening INTC261016P00087500 sell -1 net credit $1.21 gross $121.00 ...]",
    "[[ROLL] Close order 2de702fc status orderstatus.filled after 2s]",
    "[2026-08-27 14:07:05] INFO order submitted: sell -1 XOM261016P00145000",
    "[CLOSER] JNJ260918P00260000 CLOSED at 2.80 (critical=True)",
]

CHATTER_LINES = [
    "[[ROLLER] Evaluating rolling need 3% OTM + assignment avoidance debit -$0.20 override, risk $71800]",
    "[[SGOV] Sweep disabled (SGOV_ENABLED=False) - cash stays in the broker's own sweep (Robinhood/Fidelity model)]",
    "[[ROLLER] No roll targets for JNJ260918P00260000 ]",
    "[[ROLLER] Per-run roll cap (2/run): deferring 1 to next run: NEE260918P00082500]",
    "[[SWALLOWED] whatever FILLED something",  # swallowed prefix always excluded
]


def test_real_actions_kept():
    out = pv.extract_actions(REAL_ACTION_LINES)
    assert len(out) == 5


def test_chatter_not_actions():
    assert pv.extract_actions(CHATTER_LINES) == []


def test_mixed_log_separates_actions():
    out = pv.extract_actions(REAL_ACTION_LINES + CHATTER_LINES)
    assert len(out) == 5
    assert all("assignment avoidance" not in a and "Sweep disabled" not in a for a in out)


def test_health_retry_absorbs_single_drop(monkeypatch):
    monkeypatch.setenv("OPTIONABLE_URL", "https://example.invalid")
    calls = {"n": 0}

    def flaky(urlopen_req, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise http_error_marker()
        return _ok_ctx({"data": {"status": "healthy", "version": "0.17.0",
                                 "database": {"tradeCount": 33}}})

    monkeypatch.setattr(pv.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(pv.time, "sleep", lambda s: None)
    ok, detail = pv._fetch_optionable_health()
    assert ok and "healthy v0.17.0" in detail and calls["n"] == 3


def test_health_persistent_failure_reports(monkeypatch):
    monkeypatch.setenv("OPTIONABLE_URL", "https://example.invalid")

    def always_fails(urlopen_req, timeout):
        raise http_error_marker()

    monkeypatch.setattr(pv.urllib.request, "urlopen", always_fails)
    monkeypatch.setattr(pv.time, "sleep", lambda s: None)
    ok, detail = pv._fetch_optionable_health()
    assert not ok and "attempt 3/3" in detail


def http_error_marker():
    import http.client
    return http.client.RemoteDisconnected("gone")


class _ok_ctx:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return io.BytesIO(json.dumps(self._payload).encode())

    def __exit__(self, *a):
        return False
