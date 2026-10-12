"""Regression: roll fill polls must recognise Alpaca's enum status.

Bug (found 2026-10-12 execution-fills audit): the Alpaca SDK returns an
OrderStatus enum whose str() is "OrderStatus.FILLED", but roll_position()
compared the lowered string to exactly "filled". On the live paper path
the comparison could never be true:

  - every roll close poll burned its full 45s (13/13 close lines in the
    Sep-Oct 2026 cron logs read "Close status orderstatus.filled"),
  - every roll open poll burned its full 30s and fell back to the
    scan-time bid estimate for the dashboard push (11/11 roll opens
    "not confirmed filled within 30s", 0 "Open FILLED" ever), with the
    entry only corrected later by reconcile_open_entry_prices()
    (e.g. CSCO 1.74->1.72, KO 1.06->1.04, PLD 2.97->3.10).

Existing fakes returned the plain string "filled", which masked this.
These tests use the enum-shaped status the real broker returns.
"""
import datetime
import types

import core.optionable_sync as opt_sync
from core.roller import RollCandidate, RollTarget, _is_filled_status, roll_position


class _EnumLikeStatus:
    """Mimics the Alpaca SDK enum: str() is 'OrderStatus.FILLED'."""

    def __init__(self, text):
        self._text = text

    def __str__(self):
        return self._text


def test_is_filled_status_shapes():
    assert _is_filled_status("filled")
    assert _is_filled_status("FILLED")
    assert _is_filled_status(_EnumLikeStatus("OrderStatus.FILLED"))
    assert _is_filled_status("orderstatus.filled")
    # Not full fills: must keep polling.
    assert not _is_filled_status(_EnumLikeStatus("OrderStatus.PARTIALLY_FILLED"))
    assert not _is_filled_status("partially_filled")
    assert not _is_filled_status(_EnumLikeStatus("OrderStatus.NEW"))
    assert not _is_filled_status(None)
    assert not _is_filled_status("")


def _candidate():
    return RollCandidate(
        symbol="F260918P00014000", underlying="F", strike=14.0,
        expiration=datetime.date(2026, 9, 18), dte=0, qty=-1,
        avg_entry_price=1.00, current_price=0.50, underlying_price=13.50,
        delta=-0.45, bid=0.48, ask=0.52, is_put=True)


def _target():
    return RollTarget(
        symbol="F261016P00013500", strike=13.5,
        expiration=datetime.date(2026, 10, 16), dte=28, bid_price=1.56,
        ask_price=1.60, delta=-0.30, oi=1000, premium_rate=0.115,
        annualized_yield=0.05, net_credit=1.06,
        roll_type="assignment_avoidance", reasoning="test roll")


class _FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


class _EnumOrders:
    def __init__(self, fill_price):
        self._fill_price = fill_price

    def submit_order(self, req):
        return types.SimpleNamespace(id="order-1")

    def get_order_by_id(self, oid):
        return types.SimpleNamespace(
            status=_EnumLikeStatus("OrderStatus.FILLED"),
            filled_avg_price=self._fill_price)


class _EnumClient:
    def __init__(self, fill_price):
        self.trade_client = _EnumOrders(fill_price)

    def get_account(self):
        return types.SimpleNamespace(options_buying_power=100000.0)


def test_roll_recognises_enum_filled_and_pushes_actual_price(monkeypatch):
    pushed = {}
    monkeypatch.setattr(
        opt_sync, "push_trade_to_optionable",
        lambda symbol, price, **kw: pushed.update(symbol=symbol, price=price) or True)
    monkeypatch.setattr(opt_sync, "get_close_price_from_activities",
                        lambda client, sym: 0.50)
    clock = _FakeClock()
    monkeypatch.setattr("core.roller.time", clock)

    ok = roll_position(_EnumClient(fill_price=3.10), _candidate(), _target())

    assert ok is True
    assert pushed["price"] == 3.10, (
        f"dashboard must get the actual fill, not the bid estimate: {pushed}")
    # Both polls must exit on their first 2s tick once filled, not burn
    # the full 45s close + 30s open windows (75s per roll).
    elapsed = clock.now - 1_000_000.0
    assert elapsed <= 6.0, f"polls did not exit early on enum fill: {elapsed}s"
