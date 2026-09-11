"""Regression: fundamentals screen fails CLOSED, never open.

2026-09-10 audit: when every fundamentals source was down at once and the
cache was empty, evaluate_fundamentals() returned blocked=False ("No
fundamentals") and both enforcement points guard on `if fundamentals_map:`
(an empty dict is falsy), so the engine sold CSPs with the quality screen
entirely off. A quality screen must treat "no data" as unevaluated -> blocked.
"""
from unittest import mock

from core.fundamentals import evaluate_fundamentals, get_fundamentals_report


def _good_entry(de=0.4, pe=15.0):
    return {"symbol": "AAA", "PERatio": str(pe), "DebtEquity": de,
            "MarketCapitalization": "50000000000", "DividendYield": "0.02",
            "Beta": "1.1", "QuarterlyEarningsGrowthYOY": "0.05",
            "QuarterlyRevenueGrowthYOY": "0.06"}


def test_missing_symbol_is_blocked_fail_closed():
    r = evaluate_fundamentals("ZZZ", {})
    assert r["blocked"] is True
    assert "fail-closed" in r["reason"]


def test_symbol_absent_from_nonempty_map_is_blocked():
    r = evaluate_fundamentals("ZZZ", {"AAA": _good_entry()})
    assert r["blocked"] is True


def test_empty_report_blocks_every_symbol():
    with mock.patch("core.fundamentals.build_cache", return_value={}):
        report = get_fundamentals_report(["AAA", "BBB"])
    assert set(report) == {"AAA", "BBB"}
    assert all(r["blocked"] is True for r in report.values())


def test_healthy_symbol_still_passes():
    r = evaluate_fundamentals("AAA", {"AAA": _good_entry()})
    assert r["blocked"] is False


def test_extreme_leverage_still_blocked():
    r = evaluate_fundamentals("AAA", {"AAA": _good_entry(de=2.5)})
    assert r["blocked"] is True
    assert "D/E" in r["reason"]


def test_extreme_pe_still_blocked():
    r = evaluate_fundamentals("AAA", {"AAA": _good_entry(pe=60.0)})
    assert r["blocked"] is True
