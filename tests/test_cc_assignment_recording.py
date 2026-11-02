"""Covered-call assignment path: recording + sell-side guards.

2026-11-02 (Smit approved): the assignment -> covered-call phase is the
wheel's least-tested part. MP was the first assignment (Oct 30, 100 shares
@ $45.75 basis) and the first covered call (Nov 2, Dec 18 $50C @ $0.84).
These tests pin the flow end to end:

1. Optionable sync: a called-away short call must be recorded as
   'Exercised' at closePrice 0 (premium kept) -- never the estimated
   'Closed' at entry x 0.5, and never 'Expired' when the shares did not
   survive. A worthless expiry (shares still held) stays 'Expired'; a
   bought-back call keeps its real buy price; a CSP assignment stays
   'Assigned'.
2. sell_calls: never sells a call struck below the share cost-basis
   floor, never sells on fewer than 100 shares.
3. update_state: long shares + short call collapse to 'short_call', so
   the engine cannot double-sell a call on the same shares.
"""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest

import core.optionable_sync as osync
from alpaca.trading.enums import AssetClass

from core.execution import sell_calls
from core.state_manager import update_state
from core.strategy import filter_options
from models.contract import Contract
from tests.stress.fakes import FakeBrokerClient, FakeOptionContractRaw, make_occ


# ------------------------------------------------------------------ sync
class _FakeAlpacaClient:
    """Duck-typed Alpaca client (mirrors test_alpaca_sync_account_guard)."""

    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return self._positions


def _pos(symbol, asset_class):
    return SimpleNamespace(symbol=symbol, asset_class=asset_class)


def _trade(trade_id, ttype, strike, exp, entry, notes=""):
    return {"id": trade_id, "ticker": "MP", "type": ttype, "strike": strike,
            "expirationDate": exp, "entryPrice": entry, "quantity": 1,
            "commission": 0, "status": "Open", "notes": notes}


@pytest.fixture()
def _puts(monkeypatch):
    """Stub the Optionable HTTP surface; capture write PUTs."""
    monkeypatch.setattr(osync, "alive", lambda: True)
    monkeypatch.setattr(osync, "get_default_account_id", lambda: 1)
    monkeypatch.setattr(osync, "get_close_price_from_activities",
                        lambda c, occ: None)
    monkeypatch.delenv("BROKER", raising=False)
    puts = []

    def _put(url, json=None, timeout=None):
        puts.append((url, json))
        return type("R", (), {"status_code": 200, "text": "ok"})()

    monkeypatch.setattr(osync.requests, "put", _put)
    return puts


def _run_sync(monkeypatch, trades, positions):
    monkeypatch.setattr(osync, "get_optionable_open_trades",
                        lambda aid: trades)
    osync.sync_closed_trades(_FakeAlpacaClient(positions))


def test_cc_called_away_early_is_exercised_not_estimated_close(
        monkeypatch, _puts):
    """Early assignment: call gone + shares gone + no buy fill.

    Before the fix this fell into the estimated-close path ('Closed' at
    entry x 0.5) -- a phantom 50% profit on a call whose premium was
    actually kept in full.
    """
    _run_sync(monkeypatch,
              [_trade(201, "CC", 50.0, "2027-12-18", 0.84)],
              [_pos("BAC261218P00080000", "US_OPTION")])
    assert len(_puts) == 1
    payload = _puts[0][1]
    assert payload["status"] == "Exercised"
    assert payload["closePrice"] == 0.0


def test_cc_called_away_at_expiry_is_exercised_not_expired(
        monkeypatch, _puts):
    """ITM expiry: the call is auto-exercised and the shares leave.

    closePrice 0 is right either way (premium kept), but 'Expired'
    would claim the shares survived -- they did not.
    """
    _run_sync(monkeypatch,
              [_trade(202, "CC", 50.0, "2026-10-30", 0.84)],
              [_pos("BAC261218P00080000", "US_OPTION")])
    assert len(_puts) == 1
    assert _puts[0][1]["status"] == "Exercised"
    assert _puts[0][1]["closePrice"] == 0.0


def test_cc_expired_worthless_with_shares_held_stays_expired(
        monkeypatch, _puts):
    _run_sync(monkeypatch,
              [_trade(203, "CC", 50.0, "2026-10-30", 0.84)],
              [_pos("MP", "US_EQUITY")])
    assert len(_puts) == 1
    assert _puts[0][1]["status"] == "Expired"
    assert _puts[0][1]["closePrice"] == 0.0


def test_cc_bought_back_keeps_real_buy_price(monkeypatch, _puts):
    """A bought-back call has a buy fill, so it must never reach the
    Exercised branch -- the real price wins."""
    monkeypatch.setattr(osync, "get_close_price_from_activities",
                        lambda c, occ: 0.40)
    _run_sync(monkeypatch,
              [_trade(204, "CC", 50.0, "2027-12-18", 0.84,
                      notes="OCC:MP271218C00050000 syncId:xyz")],
              [_pos("MP", "US_EQUITY")])
    assert len(_puts) == 1
    assert _puts[0][1]["status"] == "Closed"
    assert _puts[0][1]["closePrice"] == 0.40


def test_csp_assignment_still_recorded_as_assigned(monkeypatch, _puts):
    """Regression guard: the new CC branch must not disturb the CSP
    assignment path that recorded MP's Oct 30 assignment."""
    _run_sync(monkeypatch,
              [_trade(205, "CSP", 50.0, "2026-10-30", 4.25)],
              [_pos("MP", "US_EQUITY")])
    assert len(_puts) == 1
    assert _puts[0][1]["status"] == "Assigned"
    assert _puts[0][1]["closePrice"] == 0.0


# ------------------------------------------------------------------ sell_calls
def _call(client, strike, bid=0.80, ask=0.90, delta=0.25, oi=500):
    exp = date.today() + timedelta(days=30)
    sym = make_occ("MP", exp, "C", strike)
    raw = FakeOptionContractRaw(sym, "MP", strike, exp, open_interest=oi)
    snap = {"latestQuote": {"bp": bid, "ap": ask}, "greeks": {"delta": delta}}
    client.option_chain.setdefault("MP", []).append((raw, snap))
    return sym


def _sell(client, price=45.75, qty=100):
    with mock.patch("core.execution.push_trade_to_optionable"):
        sell_calls(client, "MP", purchase_price=price, stock_qty=qty,
                   execution_config={"limit_enabled": False, "wait_seconds": 0})


def test_sell_calls_never_sells_below_cost_basis_floor():
    """The Nov 2 MP call cleared the $45.75 floor; a $45 strike must not
    have. The below-floor contract passes every other screen (delta,
    premium, spread, yield, OI) so only the floor rejects it."""
    c = FakeBrokerClient()
    below = _call(c, 45.0)
    above = _call(c, 50.0)
    _sell(c)
    assert c.option_sells == [above]
    assert below not in c.option_sells


def test_sell_calls_with_only_below_floor_calls_sells_nothing():
    c = FakeBrokerClient()
    _call(c, 45.0)
    _sell(c)
    assert c.option_sells == []


def test_sell_calls_under_100_shares_sells_nothing():
    c = FakeBrokerClient()
    _call(c, 50.0)
    _sell(c, qty=99)
    assert c.option_sells == []


def test_filter_options_strike_floor_is_purchase_price():
    """The floor mechanism itself, at the layer sell_calls passes
    purchase_price into: min_strike rejects below-floor strikes."""
    def _contract(strike):
        return Contract(symbol=make_occ("MP", date.today() + timedelta(days=30),
                                        "C", strike),
                        underlying="MP", strike=strike,
                        expiration=str(date.today() + timedelta(days=30)),
                        dte=30, bid_price=0.80, ask_price=0.90,
                        delta=0.25, oi=500)
    kept = filter_options([_contract(45.0), _contract(50.0)],
                          min_strike=45.75)
    assert [k.strike for k in kept] == [50.0]


# ------------------------------------------------------------------ state
def _equity(symbol, qty, avg_entry):
    return SimpleNamespace(symbol=symbol, asset_class=AssetClass.US_EQUITY,
                           qty=qty, avg_entry_price=avg_entry)


def _option(symbol):
    return SimpleNamespace(symbol=symbol, asset_class=AssetClass.US_OPTION,
                           qty=-1)


def test_update_state_long_shares_alone():
    states = update_state([_equity("MP", 100, 45.75)])
    assert states["MP"] == {"type": "long_shares", "price": 45.75, "qty": 100}


def test_update_state_shares_plus_call_is_short_call():
    """After the Nov 2 sale, MP must read 'short_call' so the next run's
    covered-call loop skips it instead of selling a second call."""
    call = make_occ("MP", date(2026, 12, 18), "C", 50.0)
    states = update_state([_equity("MP", 100, 45.75), _option(call)])
    assert states["MP"]["type"] == "short_call"
    assert states["MP"]["price"] == 45.75


def test_update_state_call_without_shares_is_invalid():
    """A short call with no shares is NOT a tolerated waiting state: the
    final validation rejects it. The engine never holds a naked call, so
    a lone short call in a position snapshot is a data error, not a
    state to carry forward."""
    call = make_occ("MP", date(2026, 12, 18), "C", 50.0)
    with pytest.raises(ValueError, match="Invalid final state"):
        update_state([_option(call)])
