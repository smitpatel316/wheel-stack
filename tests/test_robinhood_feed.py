"""Unit tests for core.robinhood_feed cross-checks — all CLI calls stubbed."""
import json
from pathlib import Path

from core.robinhood_feed import RobinhoodFeed


def _feed(tmp_path, canned: dict[str, dict]):
    """Feed whose _call returns canned[tool] payloads."""
    f = RobinhoodFeed(client_dir=tmp_path, log_path=tmp_path / "cmp.jsonl")
    f.enabled = True
    calls = []

    def fake_call(tool, args):
        calls.append(tool)
        return canned.get(tool)

    f._call = fake_call
    f._calls_seen = calls
    return f


def _lines(path):
    return [json.loads(l) for l in Path(path).read_text().splitlines()]


def test_compare_earnings_flags_discrepancies(tmp_path):
    canned = {"get_earnings_calendar": {"results": [
        {"symbol": "AAPL", "report": {"date": "2026-08-20"}},
        {"symbol": "F", "report": {"date": "2026-08-22"}},
        {"symbol": "T", "report": {"date": "2026-08-25"}},
    ]}}
    f = _feed(tmp_path, canned)
    lines = f.compare_earnings({"AAPL": "2026-08-20", "F": "2026-08-23", "XOM": "2026-08-27"})
    by_sym = {l["symbol"]: l for l in lines}
    assert "AAPL" not in by_sym            # agree -> not logged
    assert by_sym["F"]["check"] == "date_mismatch"
    assert by_sym["T"]["check"] == "rh_only"
    assert by_sym["XOM"]["check"] == "finnhub_only"
    assert all(l["kind"] == "earnings" for l in _lines(f.log_path))


def test_compare_earnings_fail_soft(tmp_path):
    f = _feed(tmp_path, {})  # tool returns None
    assert f.compare_earnings({"AAPL": "2026-08-20"})[0]["check"] == "finnhub_only"


def test_compare_fundamentals_only_logs_big_divergence(tmp_path):
    canned = {"get_equity_fundamentals": {"results": [
        {"symbol": "F", "pe_ratio": "10.0", "pb_ratio": "1.1", "market_cap": "4.0e10"},
        {"symbol": "INTC", "pe_ratio": "30.0", "pb_ratio": "1.0", "market_cap": "1.0e11"},
    ]}}
    f = _feed(tmp_path, canned)
    report = {
        "F": {"data": {"PERatio": "9.9", "PriceToBookRatio": "1.08",
                       "MarketCapitalization": "3.95e10"}},       # ~1% diffs: silent
        "INTC": {"data": {"PERatio": "10.0", "PriceToBookRatio": "1.0",
                          "MarketCapitalization": "1.0e11"}},      # PE 3x: flagged
    }
    lines = f.compare_fundamentals(report)
    assert len(lines) == 1
    assert lines[0]["symbol"] == "INTC" and lines[0]["field"] == "pe"
    assert lines[0]["diff_pct"] == 200.0


def test_compare_fundamentals_batches_by_ten(tmp_path):
    canned = {"get_equity_fundamentals": {"results": []}}
    f = _feed(tmp_path, canned)
    f.compare_fundamentals({f"S{i:02d}": {"data": {}} for i in range(23)})
    assert f._calls_seen.count("get_equity_fundamentals") == 3


def test_compare_vix(tmp_path):
    canned = {
        "get_indexes": {"indexes": [{"id": "uuid-1", "symbol": "VIX"}]},
        "get_index_quotes": {"quotes": [{"value": "15.65"}]},
    }
    f = _feed(tmp_path, canned)
    line = f.compare_vix(15.8)
    assert line["rh"] == 15.65 and line["diff"] == -0.15
    assert f.compare_vix(None) is None


def test_compare_vix_fail_soft_when_no_index_id(tmp_path):
    f = _feed(tmp_path, {"get_indexes": {"indexes": [{}]}})
    assert f.compare_vix(15.8) is None
