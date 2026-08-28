"""Regression test for reconcile_open_entry_prices.

Open trades pushed at order time carry quote prices; the broker's avg entry
is truth. 2026-08-27: JNJ entry drifted 2.08 vs broker STO 1.82 -> -$26 P/L error.
"""
import pytest
import core.optionable_sync as osync


class _Pos:
    def __init__(self, symbol, avg, ac="US_OPTION"):
        self.symbol = symbol
        self.avg_entry_price = avg
        self.asset_class = ac


class _Client:
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return self._positions


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    monkeypatch.setattr(osync, "alive", lambda: True)
    monkeypatch.setattr(osync, "get_default_account_id", lambda: 1)


def test_drifting_entry_patched_to_broker_avg(monkeypatch):
    rows = [
        {"id": 38, "ticker": "JNJ", "entryPrice": 2.08,
         "notes": "OCC:JNJ260918P00260000 via wheel-stack"},
        {"id": 44, "ticker": "XOM", "entryPrice": 1.78,
         "notes": "OCC:XOM261016P00145000 via wheel-stack"},
        {"id": 41, "ticker": "PFE", "entryPrice": 0.18,
         "notes": "OCC:FAKE991231P00027000 via wheel-stack"},  # not at broker
        {"id": 17, "ticker": "CVX", "entryPrice": 3.12, "notes": "import"},  # no OCC
    ]
    monkeypatch.setattr(osync, "get_optionable_open_trades", lambda aid: rows)
    client = _Client([
        _Pos("JNJ260918P00260000", 1.82),
        _Pos("XOM261016P00145000", 1.81),
    ])
    puts = []
    monkeypatch.setattr(osync.requests, "put",
                        lambda url, json=None, timeout=None: puts.append((url, json)) or type("R", (), {"status_code": 200})())

    patched = osync.reconcile_open_entry_prices(client)

    assert patched == 2
    assert sorted(f"{u}|{b['entryPrice']}" for u, b in puts) == [
        "http://localhost:8096/api/trades/38|1.82",
        "http://localhost:8096/api/trades/44|1.81",
    ]


def test_matching_entries_are_skipped(monkeypatch):
    rows = [{"id": 40, "ticker": "MP", "entryPrice": 0.87,
             "notes": "OCC:MP260911P00053000 via wheel-stack"}]
    monkeypatch.setattr(osync, "get_optionable_open_trades", lambda aid: rows)
    client = _Client([_Pos("MP260911P00053000", 0.868),  # within $0.005
                      _Pos("SGOV", 100.3, ac="US_EQUITY")])  # equity ignored

    monkeypatch.setattr(osync.requests, "put",
                        lambda *a, **k: pytest.fail("should not PUT"))

    assert osync.reconcile_open_entry_prices(client) == 0


def test_tracker_down_noops(monkeypatch):
    monkeypatch.setattr(osync, "alive", lambda: False)
    monkeypatch.setattr(osync.requests, "put",
                        lambda *a, **k: pytest.fail("should not PUT"))
    assert osync.reconcile_open_entry_prices(_Client([])) == 0
