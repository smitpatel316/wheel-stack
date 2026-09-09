"""Regression: Alpaca-account dashboard syncs must refuse wrong-account clients.

2026-09-08 incident: a BROKER=robinhood review-only engine run passed the
(empty) Robinhood individual-account client into sync_closed_trades(), which
read "positions I see" as "positions that exist" and FALSE-CLOSED all 10 real
Alpaca paper CSPs in the Optionable dashboard with estimated prices.

The call-site guard in scripts/run_strategy.py is not enough: the sync
functions themselves are the data-corruption gun. Every Alpaca-account sync
must verify account/mode before writing - never close/delete on absence of
data from a client that isn't the Alpaca paper account.
"""
import pytest

import core.optionable_sync as osync
import core.activities_sync as async_


class _FakeAlpacaClient:
    """Duck-typed Alpaca client stand-in (mirrors tests/test_entry_reconcile.py)."""

    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return self._positions


class RobinhoodBrokerClient:
    """Shape of the real RH adapter: a DIFFERENT account. These syncs must
    never run against it."""

    def get_positions(self):
        return []  # the empty individual account from the incident


def _open_trades():
    return [
        {"id": 101, "ticker": "BAC", "type": "CSP", "strike": 45.0,
         "expirationDate": "2026-10-16", "entryPrice": 1.20, "quantity": 1,
         "commission": 0, "status": "Open",
         "notes": "OCC:BAC261016P00045000 syncId:aaa via wheel-stack"},
        {"id": 102, "ticker": "F", "type": "CSP", "strike": 12.0,
         "expirationDate": "2026-10-16", "entryPrice": 0.45, "quantity": 1,
         "commission": 0, "status": "Open",
         "notes": "OCC:F261016P00012000 syncId:bbb via wheel-stack"},
    ]


@pytest.fixture()
def _tracker(monkeypatch):
    """Stub the Optionable HTTP surface; capture write PUTs."""
    monkeypatch.setattr(osync, "alive", lambda: True)
    monkeypatch.setattr(osync, "get_default_account_id", lambda: 1)
    monkeypatch.setattr(osync, "get_optionable_open_trades", lambda aid: _open_trades())
    puts = []

    def _put(url, json=None, timeout=None):
        puts.append((url, json))
        return type("R", (), {"status_code": 200, "text": "ok"})()

    monkeypatch.setattr(osync.requests, "put", _put)
    monkeypatch.delenv("BROKER", raising=False)
    return puts


def test_closed_trades_refused_in_robinhood_mode(_tracker, monkeypatch):
    """Exact incident replay: BROKER=robinhood + empty-account client must not
    close the real Alpaca paper trades in the dashboard."""
    monkeypatch.setenv("BROKER", "robinhood")
    client = RobinhoodBrokerClient()  # get_positions() -> []

    osync.sync_closed_trades(client)

    assert _tracker == [], "sync_closed_trades wrote closes from a Robinhood-mode run"


def test_closed_trades_refused_for_robinhood_client_even_in_alpaca_mode(_tracker, monkeypatch):
    """Defense in depth: a Robinhood client passed by any caller (wrong env,
    repurposed script) is refused regardless of the BROKER env."""
    monkeypatch.setenv("BROKER", "alpaca")

    osync.sync_closed_trades(RobinhoodBrokerClient())

    assert _tracker == [], "sync_closed_trades accepted a Robinhood client"


def test_closed_trades_still_runs_for_alpaca_client(_tracker):
    """Positive control: the real Alpaca paper client (and duck-typed fakes
    like the existing suite uses) are unaffected by the guard."""
    client = _FakeAlpacaClient([])  # no positions -> both trades look closed

    osync.sync_closed_trades(client)

    assert len(_tracker) == 2, "legit Alpaca client sync was wrongly refused"


def test_equity_sync_refused_in_robinhood_mode(monkeypatch):
    monkeypatch.setenv("BROKER", "robinhood")
    monkeypatch.setattr(osync, "alive", lambda: True)

    class _SpyClient:
        def get_positions(self):
            raise AssertionError("broker positions must not be read in RH mode")

    osync.sync_alpaca_equity_to_optionable(_SpyClient())  # must return, not raise


def test_sgov_sync_refused_in_robinhood_mode(monkeypatch):
    monkeypatch.setenv("BROKER", "robinhood")
    monkeypatch.setattr(osync, "alive", lambda: True)

    class _SpyClient:
        def get_positions(self):
            raise AssertionError("broker positions must not be read in RH mode")

    osync.sync_sgov_to_optionable(_SpyClient())


def test_realized_pnl_sync_refused_in_robinhood_mode(monkeypatch):
    monkeypatch.setenv("BROKER", "robinhood")
    monkeypatch.setattr(osync, "alive", lambda: True)

    class _SpyClient:
        @property
        def trade_client(self):
            raise AssertionError("trade client must not be touched in RH mode")

    summary = osync.sync_realized_pnl_from_alpaca(_SpyClient())
    assert summary["corrected"] == 0


def test_entry_reconcile_refused_in_robinhood_mode(monkeypatch):
    monkeypatch.setenv("BROKER", "robinhood")
    monkeypatch.setattr(osync, "alive", lambda: True)

    class _SpyClient:
        def get_positions(self):
            raise AssertionError("broker positions must not be read in RH mode")

    assert osync.reconcile_open_entry_prices(_SpyClient()) == 0


def test_activities_syncs_refused_in_robinhood_mode(monkeypatch):
    """sync_dividends_and_interest / sync_option_events must not run the
    Alpaca-activities pipeline (or the nested sync_closed_trades) in RH mode."""
    monkeypatch.setenv("BROKER", "robinhood")
    monkeypatch.setattr(async_, "fetch_activities",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("Alpaca activities must not be fetched in RH mode")))
    closed_calls = []
    monkeypatch.setattr(osync, "sync_closed_trades",
                        lambda client, *a, **k: closed_calls.append(client))

    async_.sync_dividends_and_interest(object())
    async_.sync_option_events(object())

    assert closed_calls == [], "sync_option_events drove sync_closed_trades in RH mode"
