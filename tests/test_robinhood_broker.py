"""Tests for core/robinhood_broker.py — all RH transport is stubbed."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import robinhood_broker as rb  # noqa: E402
from core.utils import parse_option_symbol  # noqa: E402


@pytest.fixture()
def live_env(monkeypatch):
    monkeypatch.setenv("RH_LIVE_ORDERS", "true")
    monkeypatch.delenv("RH_DRY_RUN", raising=False)


def _adapter(live_env=None, dry_run=False, **kw):
    return rb.RobinhoodBrokerClient(data_client=None, live=True, dry_run=dry_run)


# ---------------------------------------------------------------- gates
def test_constructor_requires_live_and_env(monkeypatch):
    monkeypatch.setenv("RH_LIVE_ORDERS", "true")
    with pytest.raises(Exception, match="RH_LIVE_ORDERS"):
        rb.RobinhoodBrokerClient(data_client=None, live=False)
    monkeypatch.delenv("RH_LIVE_ORDERS")
    with pytest.raises(Exception, match="RH_LIVE_ORDERS"):
        rb.RobinhoodBrokerClient(data_client=None, live=True)


def test_constructor_ok(live_env):
    a = _adapter()
    assert a._dry_run is False


def test_liquidate_refused(live_env):
    a = _adapter()
    with pytest.raises(Exception, match="fresh-start"):
        a.liquidate_all_positions()


# ---------------------------------------------------------------- OCC bridge
def test_occ_roundtrip_engine_parser():
    occ = rb._to_occ("F", "2026-09-25", 12.0, "put")
    und, typ, strike = parse_option_symbol(occ)
    assert (und, typ, strike) == ("F", "P", 12.0)
    occ = rb._to_occ("AAPL", "2025-05-16", 207.5, "call")
    assert parse_option_symbol(occ) == ("AAPL", "C", 207.5)


def test_is_occ():
    assert rb._is_occ("F260925P00012000")
    assert not rb._is_occ("F")
    assert not rb._is_occ("F260925X00012000")


# ---------------------------------------------------------------- order paths
def test_market_sell_dry_run_never_places(live_env):
    a = _adapter(dry_run=True)
    with mock.patch.object(rb, "find_option_id", return_value="opt-1") as f, \
         mock.patch.object(rb, "get_agentic_account",
                           return_value={"account_number": "000000005913"}), \
         mock.patch.object(rb, "dry_run_review",
                           return_value={"dry_run": True, "review_clean": True}) as r, \
         mock.patch.object(rb, "place_option_order") as p:
        view = a.market_sell("F260925P00012000", qty=2)
        f.assert_called_once()
        r.assert_called_once()
        p.assert_not_called()
        assert view.id.startswith("dry-run-")
        legs = r.call_args[0][0]
        assert legs[0]["side"] == "sell" and legs[0]["position_effect"] == "open"
        assert r.call_args[0][1] == 2


def test_market_buy_maps_to_buy_to_close(live_env):
    a = _adapter()
    with mock.patch.object(rb, "find_option_id", return_value="opt-9"), \
         mock.patch.object(rb, "get_agentic_account",
                           return_value={"account_number": "000000005913"}), \
         mock.patch.object(rb, "place_option_order",
                           return_value={"id": "o-1", "state": "queued", "legs": []}) as p:
        view = a.market_buy("F260925P00012000", qty=1)
        legs = p.call_args[0][0]
        assert legs[0] == {"option_id": "opt-9", "side": "buy",
                           "position_effect": "close", "ratio_quantity": 1}
        assert p.call_args[1]["live"] is True
        assert view.id == "o-1" and view.status == "queued"


def test_limit_sell_passes_price(live_env):
    a = _adapter()
    with mock.patch.object(rb, "find_option_id", return_value="opt-1"), \
         mock.patch.object(rb, "get_agentic_account",
                           return_value={"account_number": "000000005913"}), \
         mock.patch.object(rb, "place_option_order",
                           return_value={"id": "o-2", "state": "queued", "legs": []}) as p:
        a.limit_sell("F260925P00012000", 1.55, qty=1)
        assert p.call_args[0][2] == "limit"
        assert p.call_args[0][3] == 1.55


def test_trade_client_shim(live_env):
    a = _adapter(dry_run=True)
    with mock.patch.object(rb, "find_option_id", return_value="opt-1"), \
         mock.patch.object(rb, "get_agentic_account",
                           return_value={"account_number": "000000005913"}), \
         mock.patch.object(rb, "dry_run_review",
                           return_value={"dry_run": True, "review_clean": True}) as r:
        buy_req = SimpleNamespace(symbol="F260925P00012000", qty=1,
                                  side="buy", limit_price=None)
        a.trade_client.submit_order(buy_req)
        legs = r.call_args[0][0]
        assert legs[0]["side"] == "buy" and legs[0]["position_effect"] == "close"
        # non-OCC refused
        with pytest.raises(Exception, match="non-OCC"):
            a.trade_client.submit_order(SimpleNamespace(symbol="F", qty=1,
                                                        side="buy", limit_price=None))


def test_cancel_get_order(live_env):
    a = _adapter()
    with mock.patch.object(rb, "_rh_cancel", return_value={"ok": True}) as c, \
         mock.patch.object(rb, "get_option_order",
                           return_value={"id": "o-1", "state": "cancelled",
                                         "legs": []}):
        a.cancel_order("o-1")
        c.assert_called_once_with("o-1", live=True)
    with mock.patch.object(rb, "get_option_order",
                           return_value={"id": "o-1", "state": "filled",
                                         "legs": [{"executions": [
                                             {"quantity": "1", "price": "2.10"}]}]}):
        v = a.get_order("o-1")
        assert v.status == "filled" and abs(v.filled_avg_price - 2.10) < 1e-9


# ---------------------------------------------------------------- account / positions
def test_get_account_translation(live_env):
    a = _adapter()
    payload = {"cash": "5230.11", "equity": "187500.00", "buying_power": "9000.00"}
    with mock.patch.object(rb, "get_portfolio", return_value=payload):
        acct = a.get_account()
        assert abs(acct.cash - 5230.11) < 1e-9
        assert abs(acct.equity - 187500.00) < 1e-9
        assert abs(acct.buying_power - 9000.00) < 1e-9


def test_get_account_nested_buying_power(live_env):
    # Real RH shape: buying_power is a nested object, not a flat string.
    a = _adapter()
    payload = {"cash": "1000.00", "total_value": "1500.00",
               "buying_power": {"buying_power": "800.0000",
                                "unleveraged_buying_power": "800.0000"}}
    with mock.patch.object(rb, "get_portfolio", return_value=payload):
        acct = a.get_account()
        assert abs(acct.buying_power - 800.00) < 1e-9
        assert abs(acct.options_buying_power - 800.00) < 1e-9
        assert abs(acct.equity - 1500.00) < 1e-9


def test_get_positions_occ_translation(live_env):
    a = _adapter()
    opt_pos = [{
        "chain_symbol": "F", "expiration_date": "2026-09-25",
        "strike_price": "12.0", "type": "put",
        "quantity": "-1", "average_price": "1.42",
        "market_value": "-138.00", "current_price": "1.38",
    }]
    with mock.patch.object(rb, "_rh_option_positions", return_value=opt_pos), \
         mock.patch.object(rb, "get_agentic_account",
                           return_value={"account_number": "1"}), \
         mock.patch.object(rb, "_call_tool",
                           return_value={"positions": []}):
        poss = a.get_positions()
        assert len(poss) == 1
        p = poss[0]
        assert p.symbol == "F260925P00012000"
        assert p.qty == -1
        assert abs(p.avg_entry_price - 1.42) < 1e-9
        assert p.asset_class == "US_OPTION"


def test_get_positions_equity_included(live_env):
    a = _adapter()
    with mock.patch.object(rb, "_rh_option_positions", return_value=[]), \
         mock.patch.object(rb, "get_agentic_account",
                           return_value={"account_number": "1"}), \
         mock.patch.object(rb, "_call_tool", return_value={"positions": [{
                             "symbol": "F", "quantity": "100",
                             "average_buy_price": "11.90"}]}):
        poss = a.get_positions()
        assert len(poss) == 1
        assert poss[0].symbol == "F" and poss[0].asset_class == "US_EQUITY"


# ---------------------------------------------------------------- data delegation
def test_data_methods_need_client(live_env):
    a = _adapter()
    with pytest.raises(Exception, match="data_client"):
        a.get_options_contracts(["F"])
