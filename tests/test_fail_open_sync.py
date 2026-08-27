"""Fail-open sync (Pi migration, 2026-08-27).

Covers: durable outbox write-before-push, push failure never raising into the
engine, drain removing only acked items, idempotent re-delivery via syncId,
earnings-source pull success/failure/no-op, the last-good earnings snapshot,
and the webhook receiver's /earnings/state endpoint.

Isolation: every durable path is redirected to tmp_path and every HTTP call is
monkeypatched, EXCEPT the webhook test which runs the real handler on a
throwaway loopback server (127.0.0.1, ephemeral port) — fully local, no
external network, and never the production Optionable DB.
"""
import importlib.util
import json
import logging
import socketserver
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
import requests

import core.earnings_calendar as ec
import core.earnings_source as es
import core.optionable_sync as osync
import core.sync_outbox as so

ROOT = Path(__file__).resolve().parent.parent
OCC = "F260918P00010000"  # F 2026-09-18 $10 Put
OPENED = "2026-08-27"


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _raise(exc):
    def _f(*a, **k):
        raise exc
    return _f


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Redirect all durable state + URLs; nothing touches repo state/ or logs/."""
    monkeypatch.setenv("SYNC_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("OPTIONABLE_URL", "http://127.0.0.1:9")  # closed loopback port
    monkeypatch.delenv("EARNINGS_SOURCE_URL", raising=False)
    monkeypatch.delenv("SYNC_PUSH_TIMEOUT", raising=False)
    monkeypatch.setattr(osync, "OPTIONABLE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(ec, "CACHE_FILE", tmp_path / "earnings_cache.json")
    monkeypatch.setattr(ec, "LAST_GOOD_FILE", tmp_path / "earnings-last-good.json")
    monkeypatch.setattr(es, "STATE_FILE", tmp_path / "earnings-source-state.json")
    return tmp_path


def _outbox_files(tmp_path):
    d = tmp_path / "outbox"
    return sorted(d.glob("*.json")) if d.exists() else []


def _trade_payload(ticker="F", strike=10.0, exp="2026-09-18", sync_id=None, account_id=1):
    p = {
        "ticker": ticker,
        "type": "CSP",
        "strike": strike,
        "quantity": 1,
        "entryPrice": 0.5,
        "closePrice": 0,
        "openedDate": OPENED,
        "expirationDate": exp,
        "closedDate": None,
        "status": "Open",
        "accountId": account_id,
        "commission": 0,
        "notes": f"OCC:{OCC} syncId:{sync_id} via wheel-stack v2.5.4" if sync_id else f"OCC:{OCC}",
    }
    return p


# ---------------- outbox: write-before-push ----------------

def test_push_with_server_down_enqueues_first_and_never_raises(isolated, monkeypatch):
    monkeypatch.setattr(osync, "alive", lambda: False)
    ok = osync.push_trade_to_optionable(OCC, 0.5, contracts=1, delta=-0.3, account_id=1)
    assert ok is False
    files = _outbox_files(isolated)
    assert len(files) == 1
    item = json.loads(files[0].read_text())
    assert item["kind"] == "trade"
    assert item["method"] == "POST"
    assert item["path"] == "/api/trades"
    assert item["payload"]["ticker"] == "F"
    assert item["payload"]["type"] == "CSP"
    assert item["payload"]["strike"] == 10.0
    assert item["payload"]["expirationDate"] == "2026-09-18"
    assert item["payload"]["delta"] == 0.3  # abs() of the put delta
    assert item["id"] == so.make_trade_sync_id(OCC, OPENED)
    assert item["id"] in item["payload"]["notes"]


def test_push_timeout_leaves_item_queued_and_does_not_raise(isolated, monkeypatch):
    monkeypatch.setattr(osync, "alive", lambda: True)
    monkeypatch.setattr(so.requests, "get", lambda *a, **k: _Resp(200, {"data": []}))
    monkeypatch.setattr(so.requests, "request", _raise(requests.Timeout("simulated")))
    ok = osync.push_trade_to_optionable(OCC, 0.5, contracts=1, delta=0.3, account_id=1)
    assert ok is False
    assert len(_outbox_files(isolated)) == 1  # still queued


def test_push_connection_error_leaves_item_queued(isolated, monkeypatch):
    monkeypatch.setattr(osync, "alive", lambda: True)
    monkeypatch.setattr(so.requests, "get", lambda *a, **k: _Resp(200, {"data": []}))
    monkeypatch.setattr(so.requests, "request", _raise(requests.ConnectionError("refused")))
    ok = osync.push_trade_to_optionable(OCC, 0.5, contracts=1, delta=0.3, account_id=1)
    assert ok is False
    assert len(_outbox_files(isolated)) == 1


def test_push_success_delivers_and_clears_outbox(isolated, monkeypatch):
    monkeypatch.setattr(osync, "alive", lambda: True)
    monkeypatch.setattr(so.requests, "get", lambda *a, **k: _Resp(200, {"data": []}))
    monkeypatch.setattr(so.requests, "request", lambda *a, **k: _Resp(201, {"id": 42}))
    ok = osync.push_trade_to_optionable(OCC, 0.5, contracts=1, delta=0.3, account_id=1)
    assert ok is True
    assert _outbox_files(isolated) == []


def test_enqueue_is_idempotent_for_same_sync_id(isolated):
    sid = so.make_trade_sync_id(OCC, OPENED)
    p1 = _trade_payload(sync_id=sid)
    p2 = _trade_payload(sync_id=sid)
    p2["entryPrice"] = 9.99  # a second, different write must NOT replace the first
    assert so.enqueue_trade(p1, sid) is not None
    assert so.enqueue_trade(p2, sid) is not None
    files = _outbox_files(isolated)
    assert len(files) == 1
    assert json.loads(files[0].read_text())["payload"]["entryPrice"] == 0.5  # first write wins


# ---------------- outbox: draining ----------------

def test_drain_removes_only_acked_items(isolated, monkeypatch):
    sid1 = so.make_trade_sync_id(OCC, OPENED)
    sid2 = so.make_trade_sync_id("BAC260918P00040000", OPENED)
    so.enqueue_trade(_trade_payload(ticker="F", sync_id=sid1), sid1)
    so.enqueue_trade(_trade_payload(ticker="BAC", strike=40.0, sync_id=sid2), sid2)
    monkeypatch.setattr(so.requests, "get", lambda *a, **k: _Resp(200, {"data": []}))

    def fake_request(method, url, json=None, timeout=None):
        if json["ticker"] == "F":
            return _Resp(201, {"id": 1})
        return _Resp(500, text="server error")

    monkeypatch.setattr(so.requests, "request", fake_request)
    stats = so.drain_outbox()
    assert stats["pending"] == 2
    assert stats["delivered"] == 1
    assert stats["kept"] == 1
    remaining = _outbox_files(isolated)
    assert len(remaining) == 1
    item = json.loads(remaining[0].read_text())
    assert item["payload"]["ticker"] == "BAC"
    assert item["attempts"] == 1  # failure recorded


def test_drain_aborts_when_server_down(isolated, monkeypatch):
    sid1 = so.make_trade_sync_id(OCC, OPENED)
    sid2 = so.make_trade_sync_id("BAC260918P00040000", OPENED)
    so.enqueue_trade(_trade_payload(sync_id=sid1), sid1)
    so.enqueue_trade(_trade_payload(ticker="BAC", strike=40.0, sync_id=sid2), sid2)
    monkeypatch.setattr(so.requests, "get", _raise(requests.ConnectionError("down")))
    stats = so.drain_outbox()
    assert stats["down"] is True
    assert len(_outbox_files(isolated)) == 2  # nothing lost


def test_idempotent_redelivery_via_sync_id(isolated, monkeypatch):
    """Receiver already has the syncId (crash after ack before delete):
    re-delivery must not POST again, just clear the outbox item."""
    sid = so.make_trade_sync_id(OCC, OPENED)
    so.enqueue_trade(_trade_payload(sync_id=sid), sid)
    existing = {"id": 7, "ticker": "F", "type": "CSP", "strike": 10.0,
                "expirationDate": "2026-09-18", "notes": f"OCC:{OCC} syncId:{sid} via wheel-stack v2.5.4"}
    monkeypatch.setattr(so.requests, "get", lambda *a, **k: _Resp(200, {"data": [existing]}))
    posts = []
    monkeypatch.setattr(so.requests, "request", lambda *a, **k: posts.append((a, k)))
    stats = so.drain_outbox()
    assert stats["delivered"] == 1
    assert posts == []  # no second POST - no double record
    assert _outbox_files(isolated) == []


def test_idempotent_redelivery_via_tuple_match(isolated, monkeypatch):
    """Legacy payloads (no syncId on the receiver side) are still deduped by
    the ticker/strike/expiry/type tuple, matching the pre-outbox behavior."""
    sid = so.make_trade_sync_id(OCC, OPENED)
    so.enqueue_trade(_trade_payload(sync_id=sid), sid)
    existing = {"id": 9, "ticker": "F", "type": "CSP", "strike": 10.0,
                "expirationDate": "2026-09-18", "notes": ""}
    monkeypatch.setattr(so.requests, "get", lambda *a, **k: _Resp(200, {"data": [existing]}))
    posts = []
    monkeypatch.setattr(so.requests, "request", lambda *a, **k: posts.append((a, k)))
    assert so.drain_outbox()["delivered"] == 1
    assert posts == []
    assert _outbox_files(isolated) == []


def test_drain_duplicate_response_counts_as_acked(isolated, monkeypatch):
    sid = so.make_trade_sync_id(OCC, OPENED)
    so.enqueue_trade(_trade_payload(sync_id=sid), sid)
    monkeypatch.setattr(so.requests, "get", lambda *a, **k: _Resp(200, {"data": []}))
    monkeypatch.setattr(so.requests, "request", lambda *a, **k: _Resp(409, text="trade already exists"))
    assert so.drain_outbox()["delivered"] == 1
    assert _outbox_files(isolated) == []


def test_corrupt_outbox_item_quarantined(isolated, monkeypatch):
    d = isolated / "outbox"
    d.mkdir(parents=True)
    (d / "0000000000001-broken.json").write_text("{not json")
    stats = so.drain_outbox()
    assert (d / "0000000000001-broken.bad").exists()
    assert _outbox_files(isolated) == []


# ---------------- defaults: zero behavior change ----------------

def test_drain_with_defaults_and_empty_outbox_makes_no_network(isolated, monkeypatch):
    monkeypatch.setattr(so.requests, "get", _raise(AssertionError("no network allowed")))
    monkeypatch.setattr(so.requests, "request", _raise(AssertionError("no network allowed")))
    stats = so.drain_outbox()
    assert stats == {"pending": 0, "delivered": 0, "kept": 0, "down": False}


def test_earnings_source_unset_is_complete_noop(isolated, monkeypatch):
    monkeypatch.setattr(es.requests, "get", _raise(AssertionError("no network allowed")))
    assert es.sync_from_source() is False
    assert not (isolated / "earnings-source-state.json").exists()


# ---------------- earnings-source pull ----------------

def test_earnings_source_newer_invalidation_clears_cache(isolated, monkeypatch):
    cache = isolated / "earnings_cache.json"
    old_ts = time.time() - 3600
    cache.write_text(json.dumps({"_timestamp": old_ts,
                                 "earningsCalendar": [{"symbol": "F", "date": "2026-09-01"}]}))
    now = time.time()
    seen_urls = []

    def fake_get(url, timeout=None):
        seen_urls.append(url)
        assert timeout is not None and timeout <= 5
        return _Resp(200, {"last_invalidation": now, "events_received": 3})

    monkeypatch.setenv("EARNINGS_SOURCE_URL", "http://pi.local:8744/")
    monkeypatch.setattr(es.requests, "get", fake_get)
    assert es.sync_from_source() is True
    assert seen_urls == ["http://pi.local:8744/earnings/state"]
    assert not cache.exists()  # cleared so this run refetches from Finnhub
    state = json.loads((isolated / "earnings-source-state.json").read_text())
    assert state["last_applied_invalidation"] == now

    # Same invalidation pulled again (cache since rebuilt): no re-clear.
    cache.write_text(json.dumps({"_timestamp": time.time(), "earningsCalendar": []}))
    assert es.sync_from_source() is True
    assert cache.exists()


def test_earnings_source_older_invalidation_keeps_cache(isolated, monkeypatch):
    cache = isolated / "earnings_cache.json"
    cache.write_text(json.dumps({"_timestamp": time.time(), "earningsCalendar": []}))
    monkeypatch.setenv("EARNINGS_SOURCE_URL", "http://pi.local:8744")
    monkeypatch.setattr(es.requests, "get",
                        lambda *a, **k: _Resp(200, {"last_invalidation": time.time() - 86400}))
    assert es.sync_from_source() is True
    assert cache.exists()


def test_earnings_source_failure_keeps_cache_and_logs_loudly(isolated, monkeypatch, caplog):
    cache = isolated / "earnings_cache.json"
    cache.write_text(json.dumps({"_timestamp": time.time(), "earningsCalendar": []}))
    monkeypatch.setenv("EARNINGS_SOURCE_URL", "http://pi.local:8744")
    monkeypatch.setattr(es.requests, "get", _raise(requests.ConnectionError("pi down")))
    with caplog.at_level(logging.WARNING, logger="strategy.earnings_source"):
        assert es.sync_from_source() is False
    assert cache.exists()  # untouched - run continues with local cache
    assert "[EARNINGS-SOURCE]" in caplog.text
    assert not (isolated / "earnings-source-state.json").exists()


def test_earnings_source_non_2xx_keeps_cache(isolated, monkeypatch):
    cache = isolated / "earnings_cache.json"
    cache.write_text(json.dumps({"_timestamp": time.time(), "earningsCalendar": []}))
    monkeypatch.setenv("EARNINGS_SOURCE_URL", "http://pi.local:8744")
    monkeypatch.setattr(es.requests, "get", lambda *a, **k: _Resp(502, text="bad gateway"))
    assert es.sync_from_source() is False
    assert cache.exists()


# ---------------- earnings last-good snapshot ----------------

def test_successful_fetch_writes_last_good_snapshot(isolated, monkeypatch):
    future = (date.today() + timedelta(days=10)).isoformat()
    monkeypatch.setattr("requests.get",
                        lambda *a, **k: _Resp(200, {"earningsCalendar": [{"symbol": "F", "date": future}]}))
    m = ec.build_cache(["F"])
    assert m["F"] == date.today() + timedelta(days=10)
    snap = json.loads((isolated / "earnings-last-good.json").read_text())
    assert snap["earningsCalendar"][0]["symbol"] == "F"
    assert not (isolated / "earnings-last-good.tmp").exists()  # atomic rename completed


def test_last_good_snapshot_used_when_cache_missing(isolated, monkeypatch, caplog):
    future = (date.today() + timedelta(days=10)).isoformat()
    (isolated / "earnings-last-good.json").write_text(json.dumps({
        "_timestamp": time.time() - 100 * 3600,  # ancient - still usable
        "earningsCalendar": [{"symbol": "F", "date": future}],
    }))
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(503))  # Finnhub down
    with caplog.at_level(logging.WARNING):
        m = ec.build_cache(["F"])
    assert m["F"] == date.today() + timedelta(days=10)
    assert "last-good" in caplog.text.lower()


def test_last_good_snapshot_used_when_cache_too_stale(isolated, monkeypatch):
    future = (date.today() + timedelta(days=10)).isoformat()
    (isolated / "earnings_cache.json").write_text(json.dumps({
        "_timestamp": time.time() - 72 * 3600,  # beyond 48h stale-OK
        "earningsCalendar": [{"symbol": "ZZZ", "date": future}],
    }))
    (isolated / "earnings-last-good.json").write_text(json.dumps({
        "_timestamp": time.time() - 60 * 3600,
        "earningsCalendar": [{"symbol": "F", "date": future}],
    }))
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(503))
    m = ec.build_cache(["F"])
    assert m["F"] == date.today() + timedelta(days=10)


def test_last_good_snapshot_self_filters_past_dates(isolated, monkeypatch):
    past = (date.today() - timedelta(days=30)).isoformat()
    (isolated / "earnings-last-good.json").write_text(json.dumps({
        "_timestamp": time.time() - 200 * 3600,
        "earningsCalendar": [{"symbol": "F", "date": past}],
    }))
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(503))
    monkeypatch.setattr(ec, "get_alpha_key", lambda: "")  # no Alpha either
    m = ec.build_cache(["F"])
    assert "F" not in m  # past dates are worthless and dropped


# ---------------- webhook receiver state endpoint ----------------

def _load_webhook_module(tmp_path, monkeypatch):
    monkeypatch.setenv("FINNHUB_WEBHOOK_SECRET", "test-secret")
    spec = importlib.util.spec_from_file_location("wheel_webhook_server", ROOT / "scripts" / "webhook_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "LOGS", tmp_path)
    monkeypatch.setattr(mod, "STATE_FILE", tmp_path / "webhook_state.json")
    monkeypatch.setattr(mod, "EVENTS_LOG", tmp_path / "webhook_events.jsonl")
    monkeypatch.setattr(mod, "EARNINGS_CACHE", tmp_path / "earnings_cache.json")
    return mod


def test_webhook_state_endpoint_and_cache_clear(isolated, monkeypatch):
    mod = _load_webhook_module(isolated, monkeypatch)
    cache = isolated / "earnings_cache.json"
    cache.write_text(json.dumps({"_timestamp": time.time(), "earningsCalendar": []}))
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), mod.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # Empty state before any event
        r = requests.get(f"http://127.0.0.1:{port}/earnings/state", timeout=5)
        assert r.status_code == 200
        assert r.json() == {"last_invalidation": None, "last_event_at": None, "events_received": 0}

        # Wrong secret still rejected
        r = requests.post(f"http://127.0.0.1:{port}/webhooks/finnhub-earnings",
                          json={"x": 1}, headers={"X-Finnhub-Secret": "nope"}, timeout=5)
        assert r.status_code == 401

        # Valid event: 2xx ack, local cache cleared, invalidation recorded
        r = requests.post(f"http://127.0.0.1:{port}/webhooks/finnhub-earnings",
                          json={"x": 1}, headers={"X-Finnhub-Secret": "test-secret"}, timeout=5)
        assert r.status_code == 200

        state = None
        deadline = time.time() + 5
        while time.time() < deadline:  # ack is sent before post-processing
            state = requests.get(f"http://127.0.0.1:{port}/earnings/state", timeout=5).json()
            if state["events_received"] == 1:
                break
            time.sleep(0.05)
        assert state["events_received"] == 1
        assert state["last_invalidation"] and state["last_invalidation"] > 0
        assert state["last_event_at"]
        deadline = time.time() + 5
        while cache.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert not cache.exists()  # original local cache-clear behavior preserved

        # State survives a module reload (durable file)
        state2 = json.loads((isolated / "webhook_state.json").read_text())
        assert state2["events_received"] == 1
    finally:
        srv.shutdown()
        srv.server_close()
