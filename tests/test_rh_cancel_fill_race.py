"""Cancel-then-fill race on the main-line RH adapter.

The limit order fills in the race window between the engine's fill check
and the cancel, and the broker treats the late cancel as a no-op success
(does NOT raise). Pre-fix, cancel_order returned normally and the engine
walked into the market fallback, placing a SECOND real order for the
same contract.

All broker transport is stubbed; nothing here touches the network.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import execution  # noqa: E402
from core import robinhood_broker as rb  # noqa: E402


@pytest.fixture()
def live_env(monkeypatch):
    monkeypatch.setenv("RH_LIVE_ORDERS", "true")
    monkeypatch.delenv("RH_DRY_RUN", raising=False)


def _adapter():
    return rb.RobinhoodBrokerClient(data_client=None, live=True, dry_run=False)


def _stub_transport(monkeypatch, states, placed):
    """states: order_id -> broker state; the cancel flips it to 'filled'
    mid-call to simulate the race window."""
    monkeypatch.setattr(rb, "find_option_id", lambda *a, **k: "opt-uuid-1")

    def fake_place(legs, qty, order_type, limit_price=None, **kw):
        placed.append((order_type, limit_price))
        return {"id": "o-race", "state": "confirmed", "legs": []}

    def fake_get(oid):
        if states[oid] == "filled":
            return {"id": oid, "state": "filled", "legs": [
                {"executions": [{"quantity": "1", "price": "1.62"}]}]}
        return {"id": oid, "state": states[oid], "legs": []}

    def fake_cancel(oid, live=True):
        # Dangerous broker behavior: the cancel arrives after the fill and
        # the API reports success instead of raising.
        states[oid] = "filled"
        return {"ok": True}

    monkeypatch.setattr(rb, "place_option_order", fake_place)
    monkeypatch.setattr(rb, "get_option_order", fake_get)
    monkeypatch.setattr(rb, "_rh_cancel", fake_cancel)


def test_cancel_order_raises_when_post_cancel_poll_sees_fill(live_env, monkeypatch):
    """A cancel that lands after the fill must not return normally -- the
    engine's fallback treats a quiet return as 'safe to re-place'."""
    a = _adapter()
    placed = []
    _stub_transport(monkeypatch, {"o-race": "confirmed"}, placed)
    a.limit_sell("F260925P00012000", 1.60, qty=1)

    with pytest.raises(rb.RHOrderFilledError):
        a.cancel_order("o-race")


def test_limit_cancel_market_flow_never_double_places_on_race_fill(
        live_env, monkeypatch):
    """End-to-end through place_limit_or_market_sell: limit fills in the race
    window, cancel is a late no-op success. The engine must return the limit
    fill -- placing the market fallback would be a second real order."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    a = _adapter()
    placed = []
    _stub_transport(monkeypatch, {"o-race": "confirmed"}, placed)

    contract = SimpleNamespace(symbol="F260925P00012000",
                               bid_price=1.55, ask_price=1.65)
    result = execution.place_limit_or_market_sell(
        a, contract, enable_limit=True, wait_seconds=0)

    assert result["type"] == "limit"
    assert abs(result["price"] - 1.62) < 1e-9
    assert len(placed) == 1, (
        f"DOUBLE ORDER: market fallback placed after a filled limit: {placed}")
    assert placed[0][0] == "limit"
