"""OAuth callback secrets must not land in webhook logs (P4 audit).

Regression: Handler.log_message logged the complete request line, so
GET /oauth/robinhood/callback?code=...&state=... wrote the one-time
authorization code and state into the log stream. The relay-failure branch
also logged `target` (which carries the OAuth query) and the raw exception,
which may repeat the URL.
"""
import importlib.util
import io
import logging
import os
import sys
import urllib.error
import urllib.request

import pytest

SPEC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "webhook_server.py",
)


def load_ws(name="webhook_server_oauth"):
    spec = importlib.util.spec_from_file_location(name, SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ws():
    return load_ws()


CALLBACK_LINE = ('"GET /oauth/robinhood/callback?code=SECRET-CODE'
                 '&state=SECRET-STATE HTTP/1.1" 200 -')


def test_redact_oauth_query_strips_code_and_state(ws):
    redacted = ws._redact_oauth_query(CALLBACK_LINE)
    assert "SECRET-CODE" not in redacted
    assert "SECRET-STATE" not in redacted
    assert "/oauth/robinhood/callback" in redacted  # path itself stays for routing debug


def test_redact_oauth_query_leaves_other_paths(ws):
    line = '"GET /earnings/state?since=2026-09-01 HTTP/1.1" 200 -'
    assert ws._redact_oauth_query(line) == line


def test_log_message_redacts_callback_request_line(ws, capsys):
    h = ws.Handler.__new__(ws.Handler)
    h.address_string = lambda: "127.0.0.1"
    h.log_message(CALLBACK_LINE)
    out = capsys.readouterr().out
    assert "SECRET-CODE" not in out
    assert "SECRET-STATE" not in out
    assert "/oauth/robinhood/callback" in out


def test_relay_failure_does_not_log_query_or_url(ws, monkeypatch, caplog):
    monkeypatch.setattr(ws, "RH_RELAY_URL", "https://sink.example/oauth/robinhood/callback")

    def boom(req, timeout=None):
        raise urllib.error.URLError(f"connection refused for {req.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    h = ws.Handler.__new__(ws.Handler)
    h._html = lambda code, title, msg: None
    with caplog.at_level(logging.ERROR, logger=ws.__name__):
        h._rh_oauth_callback({"code": ["SECRET-CODE"], "state": ["SECRET-STATE"]})
    assert "SECRET-CODE" not in caplog.text
    assert "SECRET-STATE" not in caplog.text
    assert "[RH-RELAY]" in caplog.text  # the failure is still logged, minus secrets
