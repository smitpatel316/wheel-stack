"""Regression: /webhooks/finnhub-earnings must require X-Finnhub-Secret.

The receiver's documented contract (config/webhook_config.json notes, the
module docstring, and the standing memory record) is "validates
X-Finnhub-Secret". The implementation only rejected a *wrong* secret and
silently ACCEPTED requests with no secret at all - an unauthenticated write
that appends to logs/webhook_events.jsonl, bumps the invalidation state, and
DELETES logs/earnings_cache.json (forcing a refetch; hammered repeatedly it
keeps the cache empty and degrades the earnings screen to fail-open).
"""
import importlib.util
import io
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_server(monkeypatch, tmp_path):
    monkeypatch.setenv("FINNHUB_WEBHOOK_SECRET", "s3cret-test-value")
    monkeypatch.setenv("WEBHOOK_PORT", "18911")
    spec = importlib.util.spec_from_file_location(
        "webhook_server_under_test", str(REPO / "scripts" / "webhook_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Redirect all file writes into tmp so the test never touches the repo.
    logs = tmp_path / "logs"
    monkeypatch.setattr(mod, "LOGS", logs)
    monkeypatch.setattr(mod, "EVENTS_LOG", logs / "webhook_events.jsonl")
    monkeypatch.setattr(mod, "STATE_FILE", logs / "webhook_state.json")
    monkeypatch.setattr(mod, "EARNINGS_CACHE", logs / "earnings_cache.json")
    return mod


def _post(mod, headers, body=b'{"symbol":"NVDA"}'):
    h = mod.Handler.__new__(mod.Handler)
    h.headers = headers
    h.path = "/webhooks/finnhub-earnings"
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.client_address = ("127.0.0.1", 9999)
    captured = {}

    def _json(code, obj):
        captured["code"] = code
        captured["obj"] = obj

    h._json = _json
    h.do_POST()
    return captured


def test_missing_secret_rejected(monkeypatch, tmp_path):
    """No X-Finnhub-Secret header -> 401, and nothing is written."""
    mod = _load_server(monkeypatch, tmp_path)

    captured = _post(mod, {"Content-Length": "17"})

    assert captured["code"] == 401, "unauthenticated webhook POST was accepted"
    assert not (tmp_path / "logs" / "webhook_events.jsonl").exists()
    assert not (tmp_path / "logs" / "webhook_state.json").exists()


def test_wrong_secret_rejected(monkeypatch, tmp_path):
    mod = _load_server(monkeypatch, tmp_path)

    captured = _post(mod, {"Content-Length": "17",
                           "X-Finnhub-Secret": "wrong"})

    assert captured["code"] == 401
    assert not (tmp_path / "logs" / "webhook_state.json").exists()


def test_correct_secret_accepted(monkeypatch, tmp_path):
    """Positive control: the documented Finnhub flow still works."""
    mod = _load_server(monkeypatch, tmp_path)

    captured = _post(mod, {"Content-Length": "17",
                           "X-Finnhub-Secret": "s3cret-test-value"})

    assert captured["code"] == 200
    assert captured["obj"] == {"status": "received"}
    events = (tmp_path / "logs" / "webhook_events.jsonl").read_text()
    assert "NVDA" in events
    state = json.loads((tmp_path / "logs" / "webhook_state.json").read_text())
    assert state["events_received"] == 1


def test_malformed_body_rejected_without_crash(monkeypatch, tmp_path):
    """Garbage Content-Length / non-JSON body -> 400, server keeps running."""
    mod = _load_server(monkeypatch, tmp_path)

    captured = _post(mod, {"Content-Length": "not-a-number",
                           "X-Finnhub-Secret": "s3cret-test-value"},
                     body=b"\xff\xfe not json")

    assert captured["code"] == 400
