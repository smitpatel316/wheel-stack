"""Stress-suite safety harness.

CRITICAL: these tests exercise real engine functions (sell_puts, sell_calls,
rollers) with fake broker clients. Those engine functions import
push_trade_to_optionable directly from core.optionable_sync, which performs
real HTTP against OPTIONABLE_URL (default http://localhost:8096 — the
PRODUCTION Optionable instance). On 2026-08-14 the fuzz tests leaked six fake
trades (AAA, CHP, AMD, BBB x2, F CC) into the live dashboard this way.

Every network-facing function in core.optionable_sync calls alive() first, so
patching alive() to False disables all outbound Optionable traffic suite-wide.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_optionable_network(monkeypatch):
    import core.optionable_sync as osync

    monkeypatch.setattr(osync, "alive", lambda: False)
    monkeypatch.setattr(osync, "OPTIONABLE_URL", "http://127.0.0.1:9")
