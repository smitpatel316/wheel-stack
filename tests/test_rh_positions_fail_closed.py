"""Regression: RH adapter get_positions must fail closed, not understate risk.

calculate_risk() sizes every new CSP from the position list. The adapter used
to (a) silently DROP option positions it couldn't build an OCC symbol for,
(b) report qty 0 for unparseable quantities, and (c) swallow a failed equity-
positions fetch entirely — all three understate live risk, so the engine
would sell more puts than the cap allows, with only a debug log as evidence.
On the live-money path an unreadable position must abort the run, not vanish.
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import robinhood_broker as rb  # noqa: E402


@pytest.fixture()
def live_env(monkeypatch):
    monkeypatch.setenv("RH_LIVE_ORDERS", "true")
    monkeypatch.delenv("RH_DRY_RUN", raising=False)


def _adapter(**kw):
    return rb.RobinhoodBrokerClient(data_client=None, live=True, **kw)


def _pos_ctx(opt_positions, equity_result=None, equity_exc=None):
    """Patch the three transport calls get_positions() depends on."""
    call = mock.patch.object(rb, "_call_tool",
                             side_effect=equity_exc) if equity_exc else \
        mock.patch.object(rb, "_call_tool", return_value=equity_result)
    return (mock.patch.object(rb, "_rh_option_positions", return_value=opt_positions),
            mock.patch.object(rb, "get_agentic_account",
                              return_value={"account_number": "1"}),
            call)


def test_unparseable_option_position_raises_not_dropped(live_env):
    a = _adapter()
    bad = [{"chain_symbol": None, "expiration_date": "not-a-date",
            "strike_price": "xx", "type": "??",
            "quantity": "-1", "average_price": "1.0"}]
    patches = _pos_ctx(bad, equity_result={"positions": []})
    with patches[0], patches[1], patches[2]:
        with pytest.raises(rb.RHOrderError):
            a.get_positions()


def test_unparseable_qty_raises_not_zeroed(live_env):
    a = _adapter()
    bad_qty = [{"chain_symbol": "F", "expiration_date": "2026-09-25",
                "strike_price": "12.0", "type": "put",
                "quantity": "not-a-number", "average_price": "1.42"}]
    patches = _pos_ctx(bad_qty, equity_result={"positions": []})
    with patches[0], patches[1], patches[2]:
        with pytest.raises(rb.RHOrderError):
            a.get_positions()


def test_missing_qty_raises_not_zeroed(live_env):
    a = _adapter()
    no_qty = [{"chain_symbol": "F", "expiration_date": "2026-09-25",
               "strike_price": "12.0", "type": "put",
               "average_price": "1.42"}]
    patches = _pos_ctx(no_qty, equity_result={"positions": []})
    with patches[0], patches[1], patches[2]:
        with pytest.raises(rb.RHOrderError):
            a.get_positions()


def test_equity_fetch_failure_raises_not_swallowed(live_env):
    a = _adapter()
    patches = _pos_ctx([], equity_exc=RuntimeError("mcp boom"))
    with patches[0], patches[1], patches[2]:
        with pytest.raises(rb.RHOrderError):
            a.get_positions()


def test_well_formed_positions_still_returned(live_env):
    a = _adapter()
    good = [{"chain_symbol": "F", "expiration_date": "2026-09-25",
             "strike_price": "12.0", "type": "put",
             "quantity": "-1", "average_price": "1.42",
             "market_value": "-138.00", "current_price": "1.38"}]
    equity = {"positions": [{"symbol": "F", "quantity": "100",
                             "average_buy_price": "11.90"}]}
    patches = _pos_ctx(good, equity_result=equity)
    with patches[0], patches[1], patches[2]:
        poss = a.get_positions()
    assert len(poss) == 2
    assert poss[0].symbol == "F260925P00012000" and poss[0].qty == -1
    assert poss[1].symbol == "F" and poss[1].asset_class == "US_EQUITY"
