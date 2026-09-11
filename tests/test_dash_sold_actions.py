"""Scan-funnel 'sold' actions: both CSP and CC Optionable pushes must register.

The dashboard log parser only matched 'Optionable: logged CSP ...', so a
covered-call fill never appeared as sold in the scan funnel (the symbol showed
no action even though the trade was recorded). The parser now matches both
trade types.
"""
from core.optionable_dashboard_sync import EngineDashboardPush


def test_cc_push_marks_sold():
    p = EngineDashboardPush()
    p._on_line("Optionable: logged CC AAPL $250.00 exp 2026-10-16 $0.85x1 comm $0.00")
    assert p.actions["AAPL"] == ("sold", "$250.00C 10/16")


def test_csp_push_still_marks_sold():
    p = EngineDashboardPush()
    p._on_line("Optionable: logged CSP F $22.50 exp 2026-10-16 $1.20x1 comm $0.00")
    assert p.actions["F"] == ("sold", "$22.50P 10/16")
