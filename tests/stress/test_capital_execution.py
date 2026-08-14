"""Stress tests: capital accounting, margin safety, sweep math, sell_puts loop, execution."""
from datetime import date, timedelta

import pytest

from core.execution import sell_puts, calc_mid_price, place_limit_or_market_sell
from core.strategy import filter_underlying, filter_options
from core.state_manager import calculate_exposures, calculate_risk
from models.contract import Contract
from tests.stress.fakes import (FakeBrokerClient, FakeAccount, FakePosition,
                                make_put, make_occ)


def _put_client(specs, opt_bp=100_000.0, **acct_kw):
    c = FakeBrokerClient(FakeAccount(options_buying_power=opt_bp, **acct_kw))
    for u, k, bid, delta in specs:
        raw, snap = make_put(u, k, dte=30, bid=bid, ask=bid + 0.04, delta=delta, oi=500)
        c.option_chain.setdefault(u, []).append((raw, snap))
        c.stock_trades[u] = k * 1.10
    return c


class TestMarginSafety:
    """C1: never submit a CSP over options BP, even with fat margin stock BP."""

    def test_margin_inflated_stock_bp_does_not_enable_csp(self):
        # stock BP $2M (margin), options BP $5k, candidate needs $44k
        c = _put_client([("AMD", 440, 10.0, -0.25)], opt_bp=5_000,
                        buying_power=2_000_000)
        c.enforce_options_bp = True  # fake broker rejects like Alpaca would
        sell_puts(c, ["AMD"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=False)
        assert not c.option_sell_attempts, \
            "engine must pre-check options BP and never even submit an undercollateralized CSP"

    def test_csp_proceeds_when_options_bp_covers(self):
        c = _put_client([("AAA", 50, 1.0, -0.25)], opt_bp=10_000)
        c.enforce_options_bp = True
        sell_puts(c, ["AAA"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert c.option_sells


class TestCapitalAccounting:
    def test_risk_bp_decremented_per_sale(self):
        # two candidates, BP covers exactly one
        c = _put_client([("AAA", 50, 1.0, -0.25), ("BBB", 40, 1.2, -0.25)],
                        opt_bp=100_000)
        sell_puts(c, ["AAA", "BBB"], 6_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert len(c.option_sells) == 1, "risk-cap BP must gate the second sale"

    def test_zero_bp_no_orders(self):
        c = _put_client([("AAA", 50, 1.0, -0.25)])
        sell_puts(c, ["AAA"], 0,
                  execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert not c.submitted

    def test_bp_refunded_on_failure(self):
        attempts = {"n": 0}

        class FailFirst(FakeBrokerClient):
            def market_sell(self, symbol, qty=1):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise Exception("transient rejection")
                return super().market_sell(symbol, qty)

        c = _put_client([("AAA", 50, 1.0, -0.25), ("BBB", 50, 1.1, -0.25)],
                        opt_bp=100_000)
        c.__class__ = FailFirst
        sell_puts(c, ["AAA", "BBB"], 6_000,  # covers exactly one $5k CSP
                  execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert len(c.option_sells) == 1, "refunded BP must let the second candidate through"

    def test_sgov_excluded_from_risk(self):
        from alpaca.trading.enums import AssetClass
        positions = [
            FakePosition("SGOV", 1000, 100.5, 100.5, asset_class=AssetClass.US_EQUITY),
            FakePosition(make_occ("F", date.today() + timedelta(days=7), "P", 14.0),
                         -1, 0.24, 0.09, asset_class=AssetClass.US_OPTION),
        ]
        put_exp, long_stock, risk = calculate_exposures(positions)
        assert put_exp == 1400 and risk == 1400, "SGOV must not count toward risk"
        assert long_stock == 0


class TestSellPutsLoop:
    def test_score_order_best_first(self):
        # BBB has richer premium -> should score higher and sell first
        c = _put_client([("AAA", 50, 0.30, -0.25), ("BBB", 50, 2.0, -0.25)],
                        opt_bp=100_000)
        sell_puts(c, ["AAA", "BBB"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert len(c.option_sells) == 2
        assert "BBB" in c.option_sells[0], f"score order wrong: {c.option_sells}"

    def test_zero_candidates_no_crash(self):
        c = FakeBrokerClient()
        c.stock_trades = {"AAA": 50.0}
        sell_puts(c, ["AAA"], 100_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert not c.submitted

    def test_empty_price_response_aborts(self):
        c = _put_client([("AAA", 50, 1.0, -0.25)])
        c.stock_trades = {}  # data outage
        sell_puts(c, ["AAA"], 100_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert not c.option_sell_attempts

    def test_missing_symbol_in_price_response_tolerated(self):
        c = FakeBrokerClient()
        c.stock_trades = {"AAA": 50.0}  # BBB missing
        out = filter_underlying(c, ["AAA", "BBB"], 100_000)
        assert out == ["AAA"]


class TestCalcMidPrice:
    @pytest.mark.parametrize("bid,ask,expected", [
        (1.0, 1.2, 1.1),
        (0, 1.2, 1.2),      # one-sided ask
        (1.0, 0, 1.0),      # one-sided bid
        (0, 0, 0.0),
        (None, None, 0.0),
        (None, 1.5, 1.5),
    ])
    def test_edges(self, bid, ask, expected):
        c = Contract(symbol="X", bid_price=bid, ask_price=ask)
        assert calc_mid_price(c) == pytest.approx(expected)


class TestExecution:
    def _contract(self, bid=1.0, ask=1.1):
        return Contract(symbol="AAA260918P00050000", underlying="AAA",
                        strike=50.0, dte=30, bid_price=bid, ask_price=ask,
                        delta=-0.25)

    def test_limit_fills_at_mid(self):
        c = FakeBrokerClient()
        r = place_limit_or_market_sell(c, self._contract(), enable_limit=True,
                                       wait_seconds=0)
        assert r["type"] == "limit" and r["price"] == 1.05

    def test_limit_unfilled_cancels_and_market_fallback(self):
        c = FakeBrokerClient()
        c.limit_fills = False
        r = place_limit_or_market_sell(c, self._contract(), enable_limit=True,
                                       wait_seconds=0)
        assert r["type"] == "market_fallback_unfilled"
        assert len(c.cancelled) == 1, "stale limit must be cancelled"

    def test_limit_throw_falls_back_to_market(self):
        c = FakeBrokerClient()
        c.raise_on_option_sell = None

        class BadLimit(FakeBrokerClient):
            def limit_sell(self, symbol, limit_price, qty=1):
                raise Exception("exchange rejected limit")

        c.__class__ = BadLimit
        r = place_limit_or_market_sell(c, self._contract(), enable_limit=True,
                                       wait_seconds=0)
        assert r["type"] == "market_fallback"

    def test_both_fail_raises(self):
        class BadAll(FakeBrokerClient):
            def limit_sell(self, symbol, limit_price, qty=1):
                raise Exception("limit rejected")

            def market_sell(self, symbol, qty=1):
                raise Exception("market rejected too")

        c = BadAll()
        with pytest.raises(Exception, match="market rejected"):
            place_limit_or_market_sell(c, self._contract(), enable_limit=True,
                                       wait_seconds=0)

    def test_limit_price_never_below_bid(self):
        c = FakeBrokerClient()
        place_limit_or_market_sell(c, self._contract(bid=1.0, ask=1.02),
                                   enable_limit=True, wait_seconds=0)
        limit_order = [o for o in c.submitted if o.type == "limit"][0]
        assert limit_order.limit_price >= 1.0

    def test_no_quotes_goes_straight_to_market(self):
        c = FakeBrokerClient()
        r = place_limit_or_market_sell(c, self._contract(bid=0, ask=0),
                                       enable_limit=True, wait_seconds=0)
        assert r["type"] == "market"
