"""Regression tests: reconcile_optionable_vs_alpaca fallback reported all zeros.

Bug (2026-09-09 audit): get_pnl_summary_for_logging() returns the flattened
keys real_pnl_realized / real_pnl_unrealized / optionable_pnl / pnl_discrepancy
/ pnl_discrepancy_pct, but the fallback compat wrapper
reconcile_optionable_vs_alpaca() read summary.get('realized' / 'unrealized' /
'optionable' / 'discrepancy') — keys that never exist. The nightly
scripts/reconcile_pnl.py fallback path therefore always printed
realized_matched=0, unrealized=0, optionable={}, discrepancy=0, and any drift
alert built on it could never fire (silent misreport of real P/L).
"""
from core import pnl_tracker


CANNED_SUMMARY = {
    "real_pnl_realized": 52.00,
    "real_pnl_unrealized": 18.50,
    "real_pnl_fees": 0.0,
    "real_pnl_total": 70.50,
    "optionable_pnl": 568.00,
    "pnl_discrepancy": 516.00,
    "pnl_discrepancy_pct": 992.31,
    "real_pnl_trade_count": 3,
}


def _fake_summary(client):
    return dict(CANNED_SUMMARY)


def test_reconcile_uses_real_summary_keys(monkeypatch):
    monkeypatch.setattr(pnl_tracker, "get_pnl_summary_for_logging", _fake_summary)
    result = pnl_tracker.reconcile_optionable_vs_alpaca(client=object())

    assert result["alpaca"]["realized_matched"] == 52.00, (
        f"fallback reconcile must carry real realized P/L, got {result['alpaca']}"
    )
    assert result["alpaca"]["unrealized"] == 18.50
    assert result["optionable"] == 568.00, (
        f"fallback reconcile must carry optionable number, got {result['optionable']!r}"
    )
    assert result["discrepancy"]["inflated_vs_real"] == 516.00


def test_reconcile_preserves_full_summary(monkeypatch):
    monkeypatch.setattr(pnl_tracker, "get_pnl_summary_for_logging", _fake_summary)
    result = pnl_tracker.reconcile_optionable_vs_alpaca(client=object())
    assert result["summary"]["real_pnl_total"] == 70.50
    assert result["summary"]["real_pnl_trade_count"] == 3


def test_reconcile_error_shape_unchanged(monkeypatch):
    def _boom(client):
        raise RuntimeError("no broker")
    monkeypatch.setattr(pnl_tracker, "get_pnl_summary_for_logging", _boom)
    result = pnl_tracker.reconcile_optionable_vs_alpaca(client=object())
    assert result["error"] == "no broker"
    assert result["realized"] == 0
