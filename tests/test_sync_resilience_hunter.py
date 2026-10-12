"""Sync-resilience hunter fixes (swarm/sync-resilience, 2026-10-12).

1. push_trade_to_optionable ignored enqueue_trade's failure return: an
   outbox WRITE failure fell through to drain/is_queued and logged a
   false "Optionable: logged ..." success - payload neither durable nor
   delivered.
2. Outbox tuple dedupe ignored openedDate: an OPEN receiver trade from
   a different date suppressed delivery of a genuinely new fill on the
   same contract.
3. activities_sync dedupe read failed OPEN: _existing_fund_txns
   returned [] on error, so a transient read failure re-POSTed recent
   dividends/interest as duplicates.
4. alive() was single-attempt at the run gates: one transient flap
   (2026-10-01 midday) lost a whole run's reconciliation sync.
5. postrun_verify gaps: OUTBOX WRITE FAILED unseen, quarantined *.bad
   payloads counted as "outbox empty", swallowed phase failures
   invisible, healthy-with-no-tradeCount PASSed.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
import requests

import core.activities_sync as acts
import core.optionable_sync as osync
import core.sync_outbox as so

ROOT = Path(__file__).resolve().parent.parent
OCC = "F260918P00010000"
OPENED = "2026-08-27"


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNC_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setattr(osync, "OPTIONABLE_URL", "http://127.0.0.1:9")
    return tmp_path


# ---- 1. enqueue failure must not produce a false success ----

def test_enqueue_failure_direct_delivery_success(isolated, monkeypatch, caplog):
    monkeypatch.setattr(so, "enqueue_trade", lambda *a, **k: None)
    monkeypatch.setattr(osync.requests, "post", lambda *a, **k: _Resp(201, {"id": 1}))
    with caplog.at_level("INFO"):
        ok = osync.push_trade_to_optionable(OCC, 0.5, contracts=1, account_id=1,
                                           opened_date=OPENED)
    assert ok is True
    assert "direct delivery succeeded" in caplog.text


def test_enqueue_failure_and_direct_failure_is_not_logged_as_success(isolated, monkeypatch, caplog):
    monkeypatch.setattr(so, "enqueue_trade", lambda *a, **k: None)
    monkeypatch.setattr(osync.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")))
    with caplog.at_level("INFO"):
        ok = osync.push_trade_to_optionable(OCC, 0.5, contracts=1, account_id=1,
                                           opened_date=OPENED)
    assert ok is False
    assert "Optionable: logged" not in caplog.text  # the old false-success line
    assert "OUTBOX WRITE FAILED" in caplog.text


# ---- 2. tuple dedupe respects openedDate ----

def _payload(opened):
    return {"ticker": "F", "type": "CSP", "strike": 10.0, "quantity": 1,
            "entryPrice": 0.5, "openedDate": opened, "expirationDate": "2026-09-18",
            "status": "Open", "accountId": 1}


def test_tuple_match_different_opened_date_still_delivers(isolated, monkeypatch):
    sid = so.make_trade_sync_id(OCC, "2026-08-28")
    p = _payload("2026-08-28")
    p["notes"] = f"OCC:{OCC} syncId:{sid}"
    so.enqueue_trade(p, sid)
    prior = {"id": 9, "ticker": "F", "type": "CSP", "strike": 10.0,
             "expirationDate": "2026-09-18", "status": "Open",
             "openedDate": OPENED, "notes": "OCC:%s syncId:other" % OCC}
    monkeypatch.setattr(so.requests, "get", lambda *a, **k: _Resp(200, {"data": [prior]}))
    posts = []
    monkeypatch.setattr(so.requests, "request",
                        lambda *a, **k: posts.append(1) or _Resp(201, {"id": 10}))
    assert so.drain_outbox()["delivered"] == 1
    assert len(posts) == 1  # new fill was NOT suppressed by the older open trade


def test_tuple_match_same_opened_date_still_dedupes(isolated, monkeypatch):
    sid = so.make_trade_sync_id(OCC, OPENED)
    p = _payload(OPENED)
    p["notes"] = f"OCC:{OCC} syncId:{sid}"
    so.enqueue_trade(p, sid)
    prior = {"id": 9, "ticker": "F", "type": "CSP", "strike": 10.0,
             "expirationDate": "2026-09-18", "status": "Open",
             "openedDate": OPENED, "notes": ""}
    monkeypatch.setattr(so.requests, "get", lambda *a, **k: _Resp(200, {"data": [prior]}))
    posts = []
    monkeypatch.setattr(so.requests, "request", lambda *a, **k: posts.append(1))
    assert so.drain_outbox()["delivered"] == 1
    assert posts == []


# ---- 3. activities dedupe read failure is fail-closed for writes ----

def test_existing_fund_txns_failure_returns_none(monkeypatch):
    monkeypatch.setattr(acts.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")))
    assert acts._existing_fund_txns() is None


def test_dividends_sync_skips_when_dedupe_unreadable(monkeypatch):
    monkeypatch.setattr(acts, "_require_alpaca_client", lambda c: True)
    monkeypatch.setenv("OPTIONABLE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(acts, "_existing_fund_txns", lambda: None)
    calls = []
    monkeypatch.setattr(acts, "fetch_activities", lambda *a, **k: calls.append(1) or [])
    acts.sync_dividends_and_interest(object())
    assert calls == []  # aborted before fetching/posting anything


# ---- 4. alive retries ----

def test_alive_retries_absorb_one_flap(monkeypatch):
    monkeypatch.setattr(osync.time, "sleep", lambda s: None)
    state = {"n": 0}

    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise requests.ConnectionError("flap")
        return _Resp(200, {})

    monkeypatch.setattr(osync.requests, "get", flaky)
    assert osync.alive_retry(attempts=2) is True
    state["n"] = 0
    monkeypatch.setattr(osync.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")))
    assert osync.alive() is False  # default stays single-attempt


# ---- 5. postrun_verify additions ----

SPEC_PATH = ROOT / "scripts" / "postrun_verify.py"


def _load_pv(name):
    spec = importlib.util.spec_from_file_location(name, SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_pv(pv, log_path):
    pv._fetch_optionable_health = lambda: (True, "healthy v0.0 1 trades")
    sys.argv = ["postrun_verify.py", str(log_path)]
    try:
        pv.main()
    except SystemExit as e:
        return e.code
    return 0


_BASE_LOG = (
    "ENGINE-EXIT=0\n"
    "Synced positions to Optionable tracker (3 Alpaca positions)\n"
    "[DASH] pushed snapshot+scan funnel (3 symbols, 3 positions, 9 equity pts) to Optionable\n"
)


def test_verify_flags_outbox_write_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SYNC_OUTBOX_DIR", str(tmp_path / "ob"))
    pv = _load_pv("pv_hunter_writefail")
    log = tmp_path / "run.log"
    log.write_text(_BASE_LOG + "[SYNC] OUTBOX WRITE FAILED for abc (trade) - payload NOT durable\n")
    code = _run_pv(pv, log)
    out = capsys.readouterr().out
    assert "FAIL sync-integrity" in out
    assert code == 2  # DEGRADED, not OK


def test_verify_flags_swallowed_phase_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SYNC_OUTBOX_DIR", str(tmp_path / "ob"))
    pv = _load_pv("pv_hunter_phasefail")
    log = tmp_path / "run.log"
    log.write_text(_BASE_LOG + "Closer eval failed: boom\n")
    code = _run_pv(pv, log)
    out = capsys.readouterr().out
    assert "FAIL phase-failures" in out
    assert code == 2


def test_verify_counts_quarantined_bad_as_not_empty(tmp_path, monkeypatch, capsys):
    ob = tmp_path / "ob"
    ob.mkdir()
    (ob / "0001-dead.bad").write_text("{corrupt")
    monkeypatch.setenv("SYNC_OUTBOX_DIR", str(ob))
    pv = _load_pv("pv_hunter_bad")
    log = tmp_path / "run.log"
    log.write_text(_BASE_LOG)
    code = _run_pv(pv, log)
    out = capsys.readouterr().out
    assert "FAIL outbox-empty" in out
    assert ".bad" in out
    assert code == 2


def test_verify_health_without_tradecount_fails(monkeypatch):
    pv = _load_pv("pv_hunter_health")
    monkeypatch.setenv("OPTIONABLE_URL", "https://example.invalid")
    monkeypatch.setattr(pv.time, "sleep", lambda s: None)

    class _Ctx:
        def __enter__(self):
            import io
            return io.BytesIO(json.dumps(
                {"data": {"status": "healthy", "version": "9", "database": {}}}).encode())

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pv.urllib.request, "urlopen", lambda *a, **k: _Ctx())
    ok, detail = pv._fetch_optionable_health()
    assert not ok and "tradeCount" in detail
