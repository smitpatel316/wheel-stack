"""Stress tests: misc loop edges, multi-position closer, regime adaptation, MAX_RISK gate audit."""
import logging
from datetime import date, timedelta

import pytest

from core.execution import sell_puts
from core.closer import evaluate_all_for_close
from core.context_analyzer import MarketContext, adapt_params, _classify_regime, _classify_vix
from models.contract import Contract
from tests.stress.fakes import (FakeBrokerClient, FakeAccount, FakePosition,
                                make_put, make_occ)


def _mk_ctx(regime, vix=15.0, vix_level="low", tech="neutral"):
    return MarketContext(vix=vix, vix_level=vix_level, market_regime=regime,
                         technical_position=tech, volatility_level="low",
                         decision_factors={})


class TestRegimeAdaptation:
    def test_bear_shrinks_risk(self):
        o = adapt_params(_mk_ctx("bear", vix=35.0, vix_level="high"))
        assert o["MAX_RISK"] == 54_000 and o["DELTA_MAX"] == 0.25

    def test_bull_full_size(self):
        o = adapt_params(_mk_ctx("bull"))
        assert o["MAX_RISK"] == 90_000 and o["DELTA_MAX"] == 0.35

    def test_neutral_mid(self):
        o = adapt_params(_mk_ctx("neutral"))
        assert o["DELTA_MAX"] == 0.30

    def test_bull_overbought_not_aggressive(self):
        o = adapt_params(_mk_ctx("bull", tech="overbought"))
        assert o["DELTA_MAX"] == 0.30, "overbought bull must fall through to neutral"

    def test_high_vix_defensive_even_if_bull(self):
        o = adapt_params(_mk_ctx("bull", vix=30.0, vix_level="high"))
        assert o["MAX_RISK"] == 54_000

    def test_classify_vix(self):
        assert _classify_vix(12) == "low"
        assert _classify_vix(30) == "high"

    def test_classify_regime(self):
        assert _classify_regime(0.05, "low") == "bull"
        assert _classify_regime(-0.05, "high") == "bear"


class TestScoreIndexMismatch:
    """S5: put_options.index(p) must never crash the loop."""

    def test_index_mismatch_no_crash(self):
        c = FakeBrokerClient(FakeAccount(options_buying_power=100_000))
        raw, snap = make_put("AAA", 50.0, dte=30, bid=1.0, ask=1.04, delta=-0.25)
        c.option_chain["AAA"] = [(raw, snap)]
        c.stock_trades["AAA"] = 55.0

        # duplicate object identity confusion: two identical contracts
        raw2, snap2 = make_put("AAA", 50.0, dte=30, bid=1.0, ask=1.04, delta=-0.25)
        raw2.symbol = raw.symbol  # same symbol — pathological duplicate
        c.option_chain["AAA"].append((raw2, snap2))

        sell_puts(c, ["AAA"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert c.option_sells, "loop must complete despite duplicate contracts"


class TestMultiClose:
    """CL8: multiple positions -> multiple close decisions."""

    def test_two_positions_two_decisions(self):
        from alpaca.trading.enums import AssetClass
        c = FakeBrokerClient()
        exp = date.today() + timedelta(days=20)
        s1 = make_occ("AAA", exp, "P", 50.0)
        s2 = make_occ("BBB", exp, "P", 40.0)
        c.positions = [
            FakePosition(s1, -1, 1.0, 0.4, asset_class=AssetClass.US_OPTION),
            FakePosition(s2, -1, 1.0, 0.9, asset_class=AssetClass.US_OPTION),
        ]
        # snapshots for closer
        from tests.stress.fakes import FakeOptionContractRaw
        c.option_chain["AAA"] = [(FakeOptionContractRaw(s1, "AAA", 50.0, exp),
                                  {"latestQuote": {"bp": 0.38, "ap": 0.42},
                                   "greeks": {"delta": -0.2}})]
        c.option_chain["BBB"] = [(FakeOptionContractRaw(s2, "BBB", 40.0, exp),
                                  {"latestQuote": {"bp": 0.88, "ap": 0.92},
                                   "greeks": {"delta": -0.3}})]
        c.stock_trades = {"AAA": 55.0, "BBB": 44.0}
        decisions = evaluate_all_for_close(c)
        assert len(decisions) == 2
        closes = [d for d in decisions if d.should_close]
        assert len(closes) == 1 and closes[0].candidate.symbol == s1, \
            "only the 60%-profit position should close"


class TestMaxRiskGateAudit:
    """C7: run_strategy must gate new CSPs on MAX_RISK."""

    def test_max_risk_gate_present(self):
        src = open("scripts/run_strategy.py").read()
        assert "MAX_RISK" in src and "buying_power" in src
        # the risk-based BP calc must subtract current risk from MAX_RISK
        import re
        assert re.search(r"MAX_RISK\s*-\s*\w+", src) or "MAX_RISK - " in src, \
            "buying power must be MAX_RISK minus current risk"

    def test_min_bp_gate_2000(self):
        src = open("scripts/run_strategy.py").read()
        assert "buying_power >= 2000" in src

    def test_market_closed_gate(self):
        src = open("scripts/run_strategy.py").read()
        assert "is_market_open" in src and "skipping new CSP sells" in src
