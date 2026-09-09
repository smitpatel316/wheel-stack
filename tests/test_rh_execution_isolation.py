"""Regression: RH dry-run / RH-mode execution isolation.

Incident class (2026-09-08): an RH dry-run validation run corrupted
Optionable — sell_puts pushed phantom open trades for orders that were never
placed, and mutated the paper funding queue. Paper-only machinery (the
Optionable new-trade push, the T+1 FundingQueue) must never fire for the
Robinhood adapter, and a dry run must never touch the order path at all
(not even 20s of fill-polling a phantom order).
"""
from datetime import date, timedelta
from unittest import mock

from core.execution import place_limit_or_market_sell, sell_puts, sell_calls
from models.contract import Contract
from tests.stress.fakes import (FakeAccount, FakeBrokerClient, FakeOptionContractRaw,
                                make_occ, make_put)


class RHTestClient(FakeBrokerClient):
    """Fake shaped like RobinhoodBrokerClient: declares broker_name/dry_run."""
    broker_name = "robinhood"

    def __init__(self, *a, dry_run=False, **kw):
        super().__init__(*a, **kw)
        self.dry_run = dry_run


def _put_client(client, specs):
    for u, k, bid, delta in specs:
        raw, snap = make_put(u, k, dte=30, bid=bid, ask=bid + 0.04, delta=delta, oi=500)
        client.option_chain.setdefault(u, []).append((raw, snap))
        client.stock_trades[u] = k * 1.10
    return client


def _contract():
    return Contract(symbol="AAA260918P00050000", underlying="AAA", strike=50.0,
                    dte=30, bid_price=1.0, ask_price=1.1, delta=-0.25)


def _call_chain(client):
    exp = date.today() + timedelta(days=30)
    sym = make_occ("AAA", exp, "C", 55.0)
    raw = FakeOptionContractRaw(sym, "AAA", 55.0, exp, open_interest=500)
    snap = {"latestQuote": {"bp": 0.8, "ap": 0.9}, "greeks": {"delta": 0.25}}
    client.option_chain["AAA"] = [(raw, snap)]
    return sym


# ---------------------------------------------------------------- dry run
def test_dry_run_place_never_touches_order_path():
    c = RHTestClient(FakeAccount(), dry_run=True)
    r = place_limit_or_market_sell(c, _contract(), enable_limit=True, wait_seconds=8)
    assert r["type"] == "dry_run"
    assert c.submitted == [], "dry run must not submit any order"
    assert c.option_sells == []
    assert c.cancelled == []


def test_dry_run_sell_puts_no_optionable_no_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("WHEEL_FUNDING_QUEUE", str(tmp_path / "fq.json"))
    c = _put_client(RHTestClient(FakeAccount(options_buying_power=100_000), dry_run=True),
                    [("AAA", 50, 1.0, -0.25)])
    with mock.patch("core.execution.push_trade_to_optionable") as push, \
         mock.patch("core.execution.FundingQueue") as fq:
        sell_puts(c, ["AAA"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=False)
    assert c.option_sells == [], "dry run places nothing"
    push.assert_not_called()
    fq.assert_not_called()


def test_dry_run_sell_calls_no_optionable_no_orders():
    c = RHTestClient(FakeAccount(), dry_run=True)
    sym = _call_chain(c)
    with mock.patch("core.execution.push_trade_to_optionable") as push:
        sell_calls(c, "AAA", purchase_price=50.0, stock_qty=100,
                   execution_config={"limit_enabled": False, "wait_seconds": 0})
    assert c.option_sells == [], "dry run places nothing"
    assert sym not in c.option_sells
    push.assert_not_called()


# ---------------------------------------------------------------- RH live mode
def test_rh_live_sell_puts_places_but_skips_paper_machinery(tmp_path, monkeypatch):
    monkeypatch.setenv("WHEEL_FUNDING_QUEUE", str(tmp_path / "fq.json"))
    c = _put_client(RHTestClient(FakeAccount(options_buying_power=100_000), dry_run=False),
                    [("AAA", 50, 1.0, -0.25)])
    with mock.patch("core.execution.push_trade_to_optionable") as push, \
         mock.patch("core.execution.FundingQueue") as fq:
        sell_puts(c, ["AAA"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=False)
    assert len(c.option_sells) == 1, "live RH still trades"
    push.assert_not_called()  # RH trades are not paper-account trades
    fq.assert_not_called()    # the paper T+1 queue is not RH state


def test_rh_live_sell_calls_places_but_skips_optionable():
    c = RHTestClient(FakeAccount(), dry_run=False)
    _call_chain(c)
    with mock.patch("core.execution.push_trade_to_optionable") as push:
        sell_calls(c, "AAA", purchase_price=50.0, stock_qty=100,
                   execution_config={"limit_enabled": False, "wait_seconds": 0})
    # Whether or not the fake chain passes the call screens, a push must
    # never record an RH trade under the paper Optionable account.
    push.assert_not_called()


# ---------------------------------------------------------------- paper unchanged
def test_paper_sell_puts_still_pushes_and_queues(tmp_path, monkeypatch):
    monkeypatch.setenv("WHEEL_FUNDING_QUEUE", str(tmp_path / "fq.json"))
    c = _put_client(FakeBrokerClient(FakeAccount(options_buying_power=100_000)),
                    [("AAA", 50, 1.0, -0.25)])
    with mock.patch("core.execution.push_trade_to_optionable") as push:
        sell_puts(c, ["AAA"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=False)
    assert len(c.option_sells) == 1
    push.assert_called_once()  # paper behavior unchanged
