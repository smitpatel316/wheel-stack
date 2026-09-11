"""Regression: RH-mode runs must never push account state to the engine dashboard.

Incident class (2026-09-08): an RH dry-run validation run corrupted the
Optionable tracker with phantom trades. The new-trade push, the closed-trade /
equity / SGOV syncs, and the activities syncs are all broker-guarded now — but
EngineDashboardPush.push() (scripts/run_strategy.py end-of-run) still POSTs the
run's account snapshot + openPositions to Optionable with WHATEVER client the
run used. In BROKER=robinhood mode that ships the RH account's equity/cash/BP
and RH positions into the Alpaca-paper dashboard: wrong-account data presented
as the paper wheel's state.

The Optionable dashboard tracks the Alpaca paper account; RH-native dashboard
sync is future work. In RH mode the push must be skipped loudly.
"""
from types import SimpleNamespace

import core.optionable_dashboard_sync as sync
from core.optionable_dashboard_sync import EngineDashboardPush


class FakeRHClient:
    """Shaped like RobinhoodBrokerClient: broker_name/dry_run, RH positions."""
    broker_name = "robinhood"
    dry_run = True

    def get_positions(self):
        return [SimpleNamespace(
            symbol="F261011P00022500", qty=-1,
            avg_entry_price=1.20, current_price=0.80,
            market_value=-80.0, unrealized_pl=40.0,
        )]

    def get_stock_latest_trade(self, symbol):
        return {symbol: SimpleNamespace(price=22.0)}


def _capture_posts(monkeypatch):
    calls = []

    class _Resp:
        status_code = 200
        text = "ok"

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "payload": json})
        return _Resp()

    monkeypatch.setattr(sync.requests, "post", fake_post)
    return calls


def test_rh_mode_dashboard_push_skipped(monkeypatch):
    """An RH-mode run must not POST anything to the paper dashboard."""
    calls = _capture_posts(monkeypatch)
    dash = EngineDashboardPush(base_url="http://example")
    dash.snapshot = {"equity": 50000.0, "cash": 10000.0}  # RH account numbers
    ok = dash.push(FakeRHClient(), ["F"], ["F"], slot="rh-test")
    assert ok is False
    assert calls == [], "RH-mode run posted account state to the paper dashboard"


def test_rh_positions_never_reach_dashboard_payload(monkeypatch):
    """Even if a push fired, RH positions must not be among openPositions."""
    calls = _capture_posts(monkeypatch)
    dash = EngineDashboardPush(base_url="http://example")
    dash.push(FakeRHClient(), ["F"], ["F"], slot="rh-test")
    for c in calls:
        positions = (c["payload"] or {}).get("openPositions") or []
        symbols = [p.get("symbol") for p in positions]
        assert "F261011P00022500" not in symbols, \
            "RH account position leaked into the paper dashboard payload"
