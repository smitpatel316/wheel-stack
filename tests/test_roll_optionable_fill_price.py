"""Regression test: roll_position must push the ACTUAL open fill price to
Optionable, not the scan-time bid estimate.

Bug (2026-09-09 audit): roll_position() polls the open leg until filled and
logs the real fill price (2026-09-08: log claimed the $1.56 bid estimate while
the real fill was $3.80), but the Optionable dashboard push still used
target.bid_price. The dashboard entry price for every rolled leg therefore
mismatched the broker fill - a broker<->dashboard mismatch that inflates or
deflates the leg's dashboard P/L by the estimate-vs-fill spread.

core/execution.py already establishes the correct pattern:
    push_trade_to_optionable(sym, (exec_result.get("price") if exec_result else bid) or 0, ...)
"""
import datetime
import types

import core.optionable_sync as opt_sync
from core.roller import RollCandidate, RollTarget, roll_position


def _candidate():
    return RollCandidate(
        symbol="F260918P00014000",
        underlying="F",
        strike=14.0,
        expiration=datetime.date(2026, 9, 18),
        dte=0,  # critical: skips the BP pre-flight, closes anyway
        qty=-1,
        avg_entry_price=1.00,
        current_price=0.50,
        underlying_price=13.50,
        delta=-0.45,
        bid=0.48,
        ask=0.52,
        is_put=True,
    )


def _target():
    return RollTarget(
        symbol="F261016P00013500",
        strike=13.5,
        expiration=datetime.date(2026, 10, 16),
        dte=28,
        bid_price=1.56,   # scan-time estimate
        ask_price=1.60,
        delta=-0.30,
        oi=1000,
        premium_rate=0.115,
        annualized_yield=0.05,
        net_credit=1.06,
        roll_type="assignment_avoidance",
        reasoning="test roll",
    )


class _FakeOrders:
    def __init__(self, fill_price):
        self.submitted = []
        self._fill_price = fill_price

    def submit_order(self, req):
        self.submitted.append(req)
        return types.SimpleNamespace(id="order-1")

    def get_order_by_id(self, oid):
        return types.SimpleNamespace(status="filled", filled_avg_price=self._fill_price)


class _FakeClient:
    def __init__(self, fill_price):
        self.trade_client = _FakeOrders(fill_price)

    def get_account(self):
        return types.SimpleNamespace(options_buying_power=100000.0)


class _FakeClock:
    """Deterministic clock: sleep() advances it, so bounded poll loops
    (45s close/BP poll, 30s open-fill poll) terminate in microseconds
    instead of wall-clock time."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


def _patch_time(monkeypatch):
    monkeypatch.setattr("core.roller.time", _FakeClock())


def test_roll_pushes_actual_open_fill_price(monkeypatch):
    pushed = {}

    def fake_push(symbol, price, **kw):
        pushed["symbol"] = symbol
        pushed["price"] = price
        return True

    monkeypatch.setattr(opt_sync, "push_trade_to_optionable", fake_push)
    monkeypatch.setattr(opt_sync, "get_close_price_from_activities",
                        lambda client, sym: 0.50)
    _patch_time(monkeypatch)

    ok = roll_position(_FakeClient(fill_price=3.80), _candidate(), _target())

    assert ok is True
    assert pushed["symbol"] == "F261016P00013500"
    assert pushed["price"] == 3.80, (
        f"dashboard must get the actual fill $3.80, not the scan-time bid estimate: got {pushed['price']}"
    )


def test_roll_push_falls_back_to_bid_when_fill_unknown(monkeypatch):
    pushed = {}

    def fake_push(symbol, price, **kw):
        pushed["price"] = price
        return True

    class _NeverFills(_FakeOrders):
        def get_order_by_id(self, oid):
            return types.SimpleNamespace(status="new", filled_avg_price=None)

    class _Client2:
        def __init__(self):
            self.trade_client = _NeverFills(3.80)

        def get_account(self):
            return types.SimpleNamespace(options_buying_power=100000.0)

    monkeypatch.setattr(opt_sync, "push_trade_to_optionable", fake_push)
    monkeypatch.setattr(opt_sync, "get_close_price_from_activities",
                        lambda client, sym: 0.50)
    _patch_time(monkeypatch)

    ok = roll_position(_Client2(), _candidate(), _target())

    assert ok is True
    # fill never confirmed within the poll window -> keep the old estimate behavior
    assert pushed["price"] == 1.56
