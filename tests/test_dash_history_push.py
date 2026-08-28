"""Dashboard push carries equity/SGOV history blobs (Pi migration fix).

The Optionable income + benchmark charts read history from the pushed
engine_dashboard blobs when the dashboard host can't see the local logs/.
Regression coverage: history is attached, sanitized, capped, and a missing
or corrupt log file never blocks the push.
"""

import json

import core.optionable_dashboard_sync as sync
from core.optionable_dashboard_sync import EngineDashboardPush, push_now, _read_history


def _posts(monkeypatch):
    calls = {}

    class _Resp:
        status_code = 200
        text = "ok"

    def fake_post(url, json=None, timeout=None):
        calls["url"] = url
        calls["payload"] = json
        return _Resp()

    monkeypatch.setattr(sync.requests, "post", fake_post)
    return calls


def _base_pusher():
    p = EngineDashboardPush(base_url="http://example")
    p.snapshot = {"equity": 100000.0}  # ensure a non-empty push
    return p


def test_push_attaches_sanitized_history(monkeypatch, tmp_path):
    (tmp_path / "equity_history.json").write_text(json.dumps([
        {"t": "2026-08-26T14:05:00-04:00", "equity": 100000.1},
        {"t": "2026-08-27T14:05:00-04:00", "equity": 100494.84, "extra": "ignored"},
        {"bad": "row"},
        {"t": "2026-08-27T15:05:00-04:00", "equity": None},
    ]))
    (tmp_path / "sgov_history.json").write_text(json.dumps([
        {"t": "2026-08-27T14:05:00-04:00", "shares": 100.0, "avg": 100.43},
    ]))
    monkeypatch.setattr(sync, "_LOGS_DIR", tmp_path)
    calls = _posts(monkeypatch)

    p = _base_pusher()
    try:
        assert p.push(symbols_all=[]) is True
    finally:
        p.uninstall()

    eq = calls["payload"]["equityHistory"]
    assert [e["equity"] for e in eq] == [100000.1, 100494.84]
    assert all(set(e) == {"t", "equity"} for e in eq)
    sg = calls["payload"]["sgovHistory"]
    assert sg == [{"t": "2026-08-27T14:05:00-04:00", "shares": 100.0, "avg": 100.43}]


def test_push_omits_missing_or_corrupt_history(monkeypatch, tmp_path):
    (tmp_path / "equity_history.json").write_text("{ not json")
    # sgov_history.json absent entirely
    monkeypatch.setattr(sync, "_LOGS_DIR", tmp_path)
    calls = _posts(monkeypatch)

    p = _base_pusher()
    try:
        assert p.push(symbols_all=[]) is True
    finally:
        p.uninstall()

    assert "equityHistory" not in calls["payload"]
    assert "sgovHistory" not in calls["payload"]
    assert calls["payload"]["snapshot"]["equity"] == 100000.0


def test_history_capped_for_and_no_trailing_error(monkeypatch, tmp_path):
    hist = [{"t": f"2026-08-{(i % 28) + 1:02d}T14:05:00-04:00", "equity": 100000.0 + i} for i in range(1500)]
    (tmp_path / "equity_history.json").write_text(json.dumps(hist))
    monkeypatch.setattr(sync, "_LOGS_DIR", tmp_path)

    out = _read_history("equity_history.json", "equity")
    assert len(out) == 1000
    assert out[-1]["equity"] == 100000.0 + 1499  # newest entries kept


def test_push_now_include_history(monkeypatch, tmp_path):
    (tmp_path / "equity_history.json").write_text(json.dumps(
        [{"t": "2026-08-27T14:05:00-04:00", "equity": 100494.84}]))
    (tmp_path / "sgov_history.json").write_text(json.dumps(
        [{"t": "2026-08-27T14:05:00-04:00", "shares": 100.0, "avg": 100.43}]))
    monkeypatch.setattr(sync, "_LOGS_DIR", tmp_path)
    calls = _posts(monkeypatch)

    assert push_now(snapshot={"equity": 1}, base_url="http://example") is True
    assert "equityHistory" not in calls["payload"]  # default off

    assert push_now(snapshot={"equity": 1}, include_history=True, base_url="http://example") is True
    assert len(calls["payload"]["equityHistory"]) == 1
    assert len(calls["payload"]["sgovHistory"]) == 1
