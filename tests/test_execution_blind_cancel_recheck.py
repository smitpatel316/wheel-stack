"""Engine blind-cancel re-check: place_limit_or_market_sell's "fill check
failed - cancel + market to be safe" branch.

Pre-fix, after the fill check raised (transport blind) and the cancel
attempt, the engine went straight to the market fallback WITHOUT
re-verifying the order. If the limit order had actually filled while the
engine was blind, the fallback placed a SECOND order for the same
contract.

Post-fix, the engine re-checks the order once after the blind cancel;
a verifiably filled order returns the limit fill and the fallback never
fires. (If the re-check is blind too, behavior is unchanged.)

Broker-agnostic stub client: no real orders anywhere.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import execution  # noqa: E402


class BlindStubClient:
    """Fill check blind (raises), cancel blind (raises), but the order
    actually filled while we couldn't see it."""

    broker_name = "alpaca"
    dry_run = False

    def __init__(self):
        self.get_calls = 0
        self.market_sells = []

    def limit_sell(self, symbol, price, qty=1):
        return SimpleNamespace(id="o-blind")

    def get_order(self, order_id):
        self.get_calls += 1
        if self.get_calls == 1:
            raise RuntimeError("transport blip on fill check")
        return SimpleNamespace(status="filled", filled_avg_price=1.62,
                               id=order_id)

    def cancel_order(self, order_id):
        raise RuntimeError("transport blip on cancel")

    def market_sell(self, symbol, qty=1):
        self.market_sells.append(symbol)
        return SimpleNamespace(id="o-blind-2")


def test_blind_cancel_rechecks_before_market_fallback(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    client = BlindStubClient()
    contract = SimpleNamespace(symbol="F260925P00012000",
                               bid_price=1.55, ask_price=1.65)

    result = execution.place_limit_or_market_sell(
        client, contract, enable_limit=True, wait_seconds=0)

    assert result["type"] == "limit", (
        f"engine fell back to market on a filled order: {result}")
    assert abs(result["price"] - 1.62) < 1e-9
    assert client.market_sells == [], (
        f"DOUBLE ORDER: market fallback placed after a filled limit: "
        f"{client.market_sells}")


def test_blind_cancel_still_falls_back_when_recheck_blind(monkeypatch):
    """If the re-check is blind too, the old behavior is preserved (the
    broker/ledger layers are the backstop, not this branch)."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    # Fast-forward the 20s market-confirm poll: the stub never confirms.
    ticks = {"t": 1000.0}

    def fake_time():
        ticks["t"] += 30.0
        return ticks["t"]

    monkeypatch.setattr("time.time", fake_time)

    class AlwaysBlind(BlindStubClient):
        def get_order(self, order_id):
            raise RuntimeError("still blind")

    client = AlwaysBlind()
    contract = SimpleNamespace(symbol="F260925P00012000",
                               bid_price=1.55, ask_price=1.65)
    result = execution.place_limit_or_market_sell(
        client, contract, enable_limit=True, wait_seconds=0)
    assert result["type"] == "market_fallback_unfilled"
    assert client.market_sells == ["F260925P00012000"]
