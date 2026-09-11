"""Regression: roll_position must not push to Optionable in dry-run / RH mode.

Incident class (2026-09-08): an RH dry-run validation run corrupted the
Optionable tracker with phantom open trades for orders that were never placed.
sell_puts/sell_calls got a broker-mode guard for push_trade_to_optionable —
but roll_position (core/roller.py) pushes the roll's open leg with NO guard.
In RH dry-run mode the "open order" is only ever a review (nothing placed),
yet the push fires with the scan-time bid estimate: a phantom Open trade on
the paper dashboard. In RH live mode the fill is real but belongs to the RH
account, not the paper tracker.

The Optionable new-trade push is paper-account machinery; mirror the
core/execution.py guard here.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import core.optionable_sync as opt_sync
import core.roller as roller
from core.roller import RollCandidate, RollTarget, roll_position
from tests.stress.fakes import make_occ


class FakeClock:
    """Advances on sleep so the fill-poll deadlines pass in microseconds."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


class FakeRHTradeClient:
    """Mimics the RH adapter shim: submit returns a dry-run order view."""

    def submit_order(self, req):
        return SimpleNamespace(id="dry-run-abc", status="dry_run")

    def get_order_by_id(self, order_id):
        return SimpleNamespace(id=order_id, status="dry_run")


class FakeRHClient:
    """Shaped like RobinhoodBrokerClient in RH_DRY_RUN mode."""
    broker_name = "robinhood"
    dry_run = True

    def __init__(self):
        self._tc = FakeRHTradeClient()

    @property
    def trade_client(self):
        return self._tc

    def get_account(self):
        return SimpleNamespace(options_buying_power=200_000.0)


def _candidate_target():
    exp_old = date.today() + timedelta(days=10)
    exp_new = date.today() + timedelta(days=17)
    candidate = RollCandidate(
        symbol=make_occ("F", exp_old, "P", 22.5),
        underlying="F", strike=22.5, expiration=exp_old, dte=10, qty=-1,
        avg_entry_price=1.20, current_price=0.80, underlying_price=24.0,
        delta=-0.25, bid=0.78, ask=0.82, is_put=True,
        itm_pct=0.0, loss_pct=0.0, profit_pct=0.33,
    )
    target = RollTarget(
        symbol=make_occ("F", exp_new, "P", 22.0),
        strike=22.0, expiration=exp_new, dte=17,
        bid_price=1.50, ask_price=1.56, delta=-0.28, oi=400,
        premium_rate=0.068, annualized_yield=1.4, net_credit=0.70,
        roll_type="defensive", reasoning="test",
    )
    return candidate, target


def _fast_clock(monkeypatch):
    monkeypatch.setattr(roller, "time", FakeClock())


def _capture_push(monkeypatch):
    pushed = []

    def fake_push(*a, **kw):
        pushed.append((a, kw))
        return True

    monkeypatch.setattr(opt_sync, "push_trade_to_optionable", fake_push)
    monkeypatch.setattr(opt_sync, "get_close_price_from_activities", lambda *a, **k: None)
    return pushed


def test_roll_dry_run_never_pushes_to_optionable(monkeypatch):
    """RH dry-run roll: no order is ever placed, so no dashboard trade may be recorded."""
    _fast_clock(monkeypatch)
    pushed = _capture_push(monkeypatch)
    candidate, target = _candidate_target()
    ok = roll_position(FakeRHClient(), candidate, target)
    assert ok is True, "the roll itself should still run to completion"
    assert pushed == [], \
        f"dry-run roll pushed phantom trade(s) to Optionable: {pushed}"


def test_roll_rh_live_never_pushes_to_paper_dashboard(monkeypatch):
    """RH live-mode roll: the fill is real but belongs to the RH account."""
    _fast_clock(monkeypatch)
    pushed = _capture_push(monkeypatch)

    class LiveRH(FakeRHClient):
        dry_run = False

    candidate, target = _candidate_target()
    ok = roll_position(LiveRH(), candidate, target)
    assert ok is True
    assert pushed == [], \
        f"RH-mode roll pushed RH-account trade(s) to the paper dashboard: {pushed}"
