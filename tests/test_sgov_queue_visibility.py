"""SGOV sweep funding-queue visibility (swarm sgov-risk hunt, 2026-10-12).

The sweep's queue-BP protection (core/sgov_float.py) reads the funding
queue to cap buys while a T+1 queue is pending. Two visibility defects:

1. A corrupt queue file makes FundingQueue.load() set broken=True and
   return an EMPTY queue, so pending_need() reads $0 and the buy
   protection silently turns off -- nothing in the run log said so.
2. Any exception in the queue check was logged at DEBUG, invisible in
   the INFO-level cron logs, so a dead protection looked identical to
   a healthy empty queue.

The fix is logging-only (a WARNING in both cases); order decisions are
unchanged. These tests pin the visibility, not new behavior.
"""
import logging
from types import SimpleNamespace

import pytest

from core.sgov_float import sync_sgov_float

PRICE = 100.50


class _Acct:
    def __init__(self, cash=0.0, equity=0.0, buying_power=0.0,
                 options_buying_power=0.0):
        self.cash = cash
        self.equity = equity
        self.buying_power = buying_power
        self.options_buying_power = options_buying_power


class _TradeClient:
    def get_orders(self, filter=None):
        return []


class _Client:
    def __init__(self, *, equity, cash=0.0, stock_bp=0.0, opt_bp=0.0):
        self.account = _Acct(cash=cash, equity=equity, buying_power=stock_bp,
                             options_buying_power=opt_bp)
        self.positions = []
        self.trade_client = _TradeClient()

    def get_positions(self):
        return list(self.positions)

    def get_account(self):
        return self.account

    def get_stock_latest_trade(self, symbols):
        return {"SGOV": SimpleNamespace(price=PRICE)}


class _CountingOrderFn:
    def __init__(self):
        self.calls = []

    def __call__(self, client, side, qty, logger_obj=None):
        self.calls.append((side, qty))


def _hold_client():
    # equity == risk_cap -> target 0 -> hold regardless of queue state.
    return _Client(equity=50_000, cash=50_000, stock_bp=1_000_000)


def test_corrupt_queue_warns_instead_of_silent_unprotection(tmp_path, monkeypatch, caplog):
    qpath = tmp_path / "funding_queue.json"
    qpath.write_text("{not json")
    monkeypatch.setenv("WHEEL_FUNDING_QUEUE", str(qpath))
    order_fn = _CountingOrderFn()
    with caplog.at_level(logging.INFO):
        sync_sgov_float(_hold_client(), logging.getLogger("t.qvis.corrupt"),
                        equity=50_000, risk_cap=50_000,
                        enabled=True, order_fn=order_fn)
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING and "queue" in r.getMessage().lower()]
    assert warnings, [r.getMessage() for r in caplog.records]
    assert any("corrupt" in w.lower() or "broken" in w.lower() for w in warnings), warnings
    assert order_fn.calls == []


def test_queue_check_exception_warns_not_debug(tmp_path, monkeypatch, caplog):
    # An expired entry whose "need" is unparseable makes expire() raise
    # inside the sweep's queue check; that failure must surface at WARNING.
    import json
    qpath = tmp_path / "funding_queue.json"
    qpath.write_text(json.dumps({
        "entries": [{"symbol": "X261120P00010000", "underlying": "X",
                     "strike": 10.0, "expiration": "2026-11-20",
                     "need": "not-a-number", "score": 0.0,
                     "queued_at": "2026-08-17T15:05:11-04:00",
                     "valid_for": "2020-01-02"}],
        "prefunded": 0.0}))
    monkeypatch.setenv("WHEEL_FUNDING_QUEUE", str(qpath))
    order_fn = _CountingOrderFn()
    with caplog.at_level(logging.INFO):
        sync_sgov_float(_hold_client(), logging.getLogger("t.qvis.exc"),
                        equity=50_000, risk_cap=50_000,
                        enabled=True, order_fn=order_fn)
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING and "queue" in r.getMessage().lower()]
    assert warnings, [r.getMessage() for r in caplog.records]
    assert order_fn.calls == []
