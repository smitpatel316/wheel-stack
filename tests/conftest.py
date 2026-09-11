"""Shared test fixtures.

The Robinhood adapter persists an order-intent ledger at
state/order_intents.db by default. Point it at a per-test temp DB so the
suite never touches (or pollutes) real engine state.
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_order_intent_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDER_INTENT_DB", str(tmp_path / "order_intents.db"))
    yield
    monkeypatch.delenv("ORDER_INTENT_DB", raising=False)
