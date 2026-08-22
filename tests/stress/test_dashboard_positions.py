"""Open Positions table payload: collect_open_positions() (2026-08-22).

Covers the engine side of the Optionable "Open Positions" table: OCC parse ->
row shape, DTE math, SGOV cash-sweep row, null otmPct when the underlying
price can't be fetched, roll-count mapping, funding-queue passthrough, and
the push() payload wiring (network stubbed — never hits the live dashboard).
"""
import json
from datetime import date
from types import SimpleNamespace

import core.optionable_dashboard_sync as dash
from core.optionable_dashboard_sync import EngineDashboardPush, collect_open_positions

TODAY = date(2026, 8, 22)  # Saturday; DTE math is calendar days


def _pos(symbol, qty, side, avg, cur, pl, plpc, mv=None):
    return SimpleNamespace(
        symbol=symbol, qty=str(qty), side=side,
        avg_entry_price=str(avg), current_price=str(cur),
        market_value=str(mv if mv is not None else abs(float(qty)) * float(cur)),
        unrealized_pl=str(pl), unrealized_plpc=str(plpc),
    )


class FakeClient:
    def __init__(self, positions, prices=None):
        self._positions = positions
        self._prices = prices or {}

    def get_positions(self):
        return self._positions

    def get_stock_latest_trade(self, symbol):
        if symbol not in self._prices:
            raise KeyError(symbol)
        return {symbol: SimpleNamespace(price=self._prices[symbol])}


def test_occ_short_put_row_shape():
    client = FakeClient(
        [_pos("INTC260918P00090000", -1, "short", 2.50, 4.00, -150.0, -0.60)],
        prices={"INTC": 88.0},
    )
    rows, fq = collect_open_positions(client, roll_counts={}, funding_entries=[], today=TODAY)
    assert fq == []
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "INTC260918P00090000"
    assert r["underlying"] == "INTC"
    assert r["type"] == "CSP"
    assert r["strike"] == 90.0
    assert r["expiry"] == "2026-09-18"
    assert r["dte"] == 27
    assert r["contracts"] == 1
    assert r["entryPrice"] == 2.50
    assert r["currentPrice"] == 4.00
    assert r["unrealizedPL"] == -150.0
    assert r["unrealizedPLpct"] == -60.0
    # put OTM%: (88 - 90) / 88 -> ITM, must be negative
    assert r["otmPct"] == round((88.0 - 90.0) / 88.0 * 100, 2)
    assert r["otmPct"] < 0


def test_short_call_otm_positive():
    client = FakeClient(
        [_pos("AAPL260911C00300000", -2, "short", 1.0, 0.5, 100.0, 0.5)],
        prices={"AAPL": 250.0},
    )
    rows, _ = collect_open_positions(client, roll_counts={}, funding_entries=[], today=TODAY)
    r = rows[0]
    assert r["type"] == "CC"
    assert r["contracts"] == 2
    # call OTM%: (300 - 250) / 250 = +20%
    assert r["otmPct"] == 20.0


def test_sgov_row_is_cash_sweep():
    client = FakeClient([_pos("SGOV", 610, "long", 100.50, 100.62, 73.2, 0.0012)])
    rows, _ = collect_open_positions(client, roll_counts={}, funding_entries=[], today=TODAY)
    r = rows[0]
    assert r["type"] == "SGOV"
    assert r["label"] == "cash sweep"
    assert r["strike"] is None and r["expiry"] is None and r["dte"] is None
    assert r["otmPct"] is None
    assert r["contracts"] == 610


def test_plain_stock_row():
    client = FakeClient([_pos("MP", 100, "long", 50.0, 55.0, 500.0, 0.10)])
    rows, _ = collect_open_positions(client, roll_counts={}, funding_entries=[], today=TODAY)
    assert rows[0]["type"] == "STOCK"
    assert rows[0]["otmPct"] is None


def test_missing_underlying_price_gives_null_otm():
    client = FakeClient(
        [_pos("INTC260918P00090000", -1, "short", 2.50, 4.00, -150.0, -0.60)],
        prices={},  # fetch raises -> null
    )
    rows, _ = collect_open_positions(client, roll_counts={}, funding_entries=[], today=TODAY)
    assert rows[0]["type"] == "CSP"
    assert rows[0]["otmPct"] is None


def test_roll_count_mapping():
    client = FakeClient(
        [_pos("BAC261016P00057500", -1, "short", 1.0, 1.0, 0.0, 0.0)],
        prices={"BAC": 60.0},
    )
    rows, _ = collect_open_positions(
        client, roll_counts={"BAC:P": 2, "KO:P": 1}, funding_entries=[], today=TODAY)
    assert rows[0]["rollsUsed"] == 2
    assert rows[0]["rollsMax"] == 2
    # no badge when the lineage isn't in the counts
    rows2, _ = collect_open_positions(client, roll_counts={}, funding_entries=[], today=TODAY)
    assert "rollsUsed" not in rows2[0]


def test_funding_queue_passthrough_explicit():
    entries = [{"symbol": "BAC261016P00057500", "underlying": "BAC", "strike": 57.5,
                "need": 5750.0, "score": 0.047, "expiration": "2026-10-16",
                "queued_at": "2026-08-20T19:06:29+00:00", "valid_for": "2026-08-21"}]
    _, fq = collect_open_positions(FakeClient([]), roll_counts={},
                                   funding_entries=entries, today=TODAY)
    assert fq == [{"symbol": "BAC261016P00057500", "underlying": "BAC", "strike": 57.5,
                   "need": 5750.0, "queued_at": "2026-08-20T19:06:29+00:00",
                   "valid_for": "2026-08-21"}]


def test_funding_queue_loads_via_env_path(tmp_path, monkeypatch):
    # stress conftest points WHEEL_FUNDING_QUEUE at tmp_path; write real state there
    import os
    path = os.environ["WHEEL_FUNDING_QUEUE"]
    with open(path, "w") as f:
        json.dump({"entries": [{"symbol": "F261016P00013000", "underlying": "F",
                                "strike": 13.0, "need": 1277.0,
                                "queued_at": "2026-08-21T14:00:00+00:00",
                                "valid_for": "2026-08-24"}], "prefunded": 0.0}, f)
    _, fq = collect_open_positions(FakeClient([]), roll_counts={}, today=TODAY)
    assert len(fq) == 1
    assert fq[0]["symbol"] == "F261016P00013000"
    assert fq[0]["need"] == 1277.0


def test_collect_never_raises_on_garbage():
    client = FakeClient([SimpleNamespace(symbol=None), SimpleNamespace()])
    rows, fq = collect_open_positions(client, roll_counts={}, funding_entries=[{"bogus": 1}], today=TODAY)
    assert rows == []
    assert fq[0]["symbol"] is None  # row still emitted, fields null


def test_push_includes_positions_and_queue(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(dash.requests, "post", fake_post)
    client = FakeClient(
        [_pos("INTC260918P00090000", -1, "short", 2.5, 4.0, -150.0, -0.6)],
        prices={"INTC": 88.0},
    )
    p = EngineDashboardPush(base_url="http://127.0.0.1:9")
    p.snapshot = {"equity": 100000.0}
    ok = p.push(client=client, symbols_all=["INTC"], allowed_symbols=["INTC"], slot="test")
    assert ok is True
    assert "openPositions" in captured["payload"]
    assert "fundingQueue" in captured["payload"]
    assert captured["payload"]["openPositions"][0]["underlying"] == "INTC"
    assert captured["payload"]["snapshot"]["equity"] == 100000.0


def test_push_survives_position_collection_failure(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        fake_post.payload = json
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(dash.requests, "post", fake_post)

    class ExplodingClient:
        def get_positions(self):
            raise RuntimeError("broker down")

    p = EngineDashboardPush(base_url="http://127.0.0.1:9")
    p.snapshot = {"equity": 1.0}
    ok = p.push(client=ExplodingClient(), symbols_all=[], allowed_symbols=[], slot="test")
    assert ok is True
    assert fake_post.payload["openPositions"] == []
    assert fake_post.payload["fundingQueue"] == []
