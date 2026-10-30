"""Roll-only fetch window (2026-10-30, Smit approved).

Live incident: CSCO Dec 18 $105P (DTE 49) found 0 roll targets in all 3
runs on 2026-10-30 -- all 307 fetched contracts dropped dte_ext<7d because
the broker fetch caps expiries at EXPIRATION_MAX=60d while the qualifying
January expiry (DTE 77) sat outside the window. The fix widens ONLY the
roll search's fetch to ROLL_EXPIRATION_MAX=95; the CSP scan keeps 60d
(the global 60->95 widening stays retracted).

These tests pin: (1) the broker request window defaults to 60d and honors
the override, (2) the roller's own filters already accept a DTE-77 January
target once it is fetched -- so the fetch window was the sole blocker.
"""
import datetime
from datetime import date, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from config.params import EXPIRATION_MAX, ROLL_EXPIRATION_MAX
from core.broker_client import BrokerClient
from core.roller import (
    RollCandidate,
    evaluate_roll_need,
    find_roll_targets,
)
from models.contract import Contract


def _ny_today():
    return datetime.datetime.now(ZoneInfo("America/New_York")).date()


def _broker_with_capturing_trade_client():
    bc = BrokerClient.__new__(BrokerClient)
    captured = {}

    class FakeTradeClient:
        def get_option_contracts(self, req):
            captured["req"] = req
            return SimpleNamespace(option_contracts=[], next_page_token=None)

    bc.trade_client = FakeTradeClient()
    return bc, captured


def test_default_fetch_window_stays_60d():
    # The CSP scan and every pre-existing caller keep the 60d window.
    bc, captured = _broker_with_capturing_trade_client()
    assert EXPIRATION_MAX == 60
    bc.get_options_contracts(["CSCO"], "put")
    assert captured["req"].expiration_date_lte == _ny_today() + timedelta(days=60)


def test_roll_fetch_override_reaches_95d():
    bc, captured = _broker_with_capturing_trade_client()
    bc.get_options_contracts(["CSCO"], "put", max_expiration_days=ROLL_EXPIRATION_MAX)
    assert captured["req"].expiration_date_lte == _ny_today() + timedelta(days=95)


def test_roll_window_policy_wider_than_scan():
    assert ROLL_EXPIRATION_MAX > EXPIRATION_MAX


def _csco_candidate():
    # CSCO Dec 18 $105P as it stood 2026-10-30: DTE 49, entry $1.80,
    # close cost $7.40 (from the live log lines).
    exp = date.today() + timedelta(days=49)
    return RollCandidate(
        symbol="CSCO261218P00105000", underlying="CSCO", strike=105.0,
        expiration=exp, dte=49, qty=-1, avg_entry_price=1.80,
        current_price=7.40, underlying_price=97.6, delta=-0.40,
        bid=7.40, ask=7.45, is_put=True, itm_pct=0.074,
        loss_pct=(7.40 - 1.80) / 1.80, profit_pct=0.0)


def _contract(dte, strike=105.0, bid=8.00, delta=-0.40):
    return Contract(symbol=f"CSCO_P_{dte}", underlying="CSCO", strike=strike,
                    dte=dte, bid_price=bid, ask_price=bid + 0.10,
                    delta=delta, oi=500)


_ROLL_CONFIG = {
    "min_credit": 0.10,  # credit-only in ALL cases (2026-10-16, Smit)
    "dte_extension_min": 7,
    "dte_extension_max": 21,
    "exp_min": 14,
    "spread_max_abs": 0.15,
    "spread_max_pct": 0.12,
}


def test_csco_january_target_passes_once_fetched():
    # The DTE-77 January contract survives every roller filter: extension
    # 28d clears the 7d minimum and stays under the 51d too-far headroom
    # (dte_extension_max 21 + 30), same-strike clears the B7 ceiling, and
    # the $0.60 net credit clears the credit-only floor.
    cand = _csco_candidate()
    decision = evaluate_roll_need(cand)
    targets = find_roll_targets(cand, [_contract(dte=77)], decision,
                                config=_ROLL_CONFIG)
    assert [t.dte for t in targets] == [77]


def test_csco_in_window_contracts_still_empty():
    # Control: replaying the live 2026-10-30 chain -- every fetched contract
    # inside the old 60d window fails the 7d extension test, so the roller
    # still correctly returns nothing for those. The fix is the fetch, not
    # a loosened filter.
    cand = _csco_candidate()
    decision = evaluate_roll_need(cand)
    assert find_roll_targets(cand, [_contract(dte=50)], decision,
                             config=_ROLL_CONFIG) == []
