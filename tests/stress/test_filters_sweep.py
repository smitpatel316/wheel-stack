"""Stress tests: option/underlying filters, sell_calls, SGOV sweep, IS_PAPER audit."""
import logging
from datetime import date, timedelta

import pytest

from core.strategy import filter_underlying, filter_options, score_options, select_options
from core.execution import sell_calls
from core.earnings_calendar import is_earnings_risk
from models.contract import Contract
from tests.stress.fakes import FakeBrokerClient, FakeAccount, FakePosition, make_put, make_occ
from config.params import (DELTA_MIN, DELTA_MAX, MIN_PREMIUM, YIELD_MIN,
                           OPEN_INTEREST_MIN, MAX_RISK)


def _c(strike=50.0, dte=30, bid=1.0, ask=1.1, delta=-0.25, oi=500,
       underlying="XYZ"):
    return Contract(symbol=f"{underlying}_sim", underlying=underlying,
                    strike=strike, dte=dte, bid_price=bid, ask_price=ask,
                    delta=delta, oi=oi)


class TestFilterOptions:
    def test_f1_reject_tally_sums(self, capsys):
        opts = [_c(delta=None), _c(delta=-0.05), _c(bid=0.05), _c(ask=None)]
        out = filter_options(opts)
        captured = capsys.readouterr().out
        assert out == [] and "rejected all 4 contracts" in captured

    def test_f2_delta_band(self):
        assert filter_options([_c(delta=-(DELTA_MIN - 0.01))]) == []
        assert filter_options([_c(delta=-(DELTA_MAX + 0.01))]) == []
        assert len(filter_options([_c(delta=-0.25)])) == 1

    def test_f2b_vol_map_delta_max_override(self):
        # tighter band from vol map
        vm = {"XYZ": {"delta_max": 0.20}}
        assert filter_options([_c(delta=-0.30)], vol_map=vm) == []
        assert len(filter_options([_c(delta=-0.19)], vol_map=vm)) == 1

    def test_f3_premium_floor(self):
        assert filter_options([_c(bid=MIN_PREMIUM - 0.01)]) == []

    def test_f4_buckets(self, capsys):
        filter_options([_c(delta=None)])
        assert "no_delta" in capsys.readouterr().out
        filter_options([_c(ask=None)])
        assert "no_ask" in capsys.readouterr().out
        filter_options([_c(oi=OPEN_INTEREST_MIN - 1)])
        assert "'oi': 1" in capsys.readouterr().out
        # strike=None is caught by the yield bucket first (yield=0 < YIELD_MIN);
        # the strike bucket only fires for strikes below min_strike
        filter_options([_c(strike=None)])
        assert "'yield': 1" in capsys.readouterr().out

    def test_f5_yield_bounds(self):
        # yield = bid/strike * 365/(dte+1); strike 50 dte 30 bid 1.0 -> 0.235 OK
        assert len(filter_options([_c(bid=1.0, strike=50.0, dte=30)])) == 1
        # tiny yield
        assert filter_options([_c(bid=0.21, strike=50.0, dte=59)]) == []
        # extreme yield
        assert filter_options([_c(bid=10.0, strike=50.0, dte=14)]) == []

    def test_wide_spread_rejected(self, capsys):
        filter_options([_c(bid=1.0, ask=1.5)])  # spread .50
        assert "spread" in capsys.readouterr().out


class TestEarningsFilter:
    def test_f6_block_within_window(self):
        today = date.today()
        m = {"AAA": today + timedelta(days=2)}
        blocked, _ = is_earnings_risk("AAA", m, today, block_days=3, dte=21)
        assert blocked

    def test_f6b_block_within_dte(self):
        today = date.today()
        m = {"AAA": today + timedelta(days=15)}
        blocked, _ = is_earnings_risk("AAA", m, today, block_days=3, dte=21)
        assert blocked, "earnings inside the DTE window must block"

    def test_f6c_allowed_after_earnings(self):
        today = date.today()
        m = {"AAA": today - timedelta(days=1)}
        blocked, _ = is_earnings_risk("AAA", m, today, block_days=3, dte=21)
        assert not blocked, "post-earnings must be allowed (CSCO Aug 13 case)"

    def test_f6d_integration_in_underlying_filter(self):
        c = FakeBrokerClient()
        c.stock_trades = {"AAA": 50.0, "BBB": 50.0}
        em = {"AAA": date.today() + timedelta(days=1)}
        out = filter_underlying(c, ["AAA", "BBB"], 100_000, earnings_map=em)
        assert out == ["BBB"]


class TestFundamentalsLiquidity:
    def test_f7_blocked_fundamentals(self):
        c = FakeBrokerClient()
        c.stock_trades = {"BAD": 50.0, "OK": 50.0}
        fm = {"BAD": {"blocked": True, "reason": "P/E 61 > 25x2"}}
        out = filter_underlying(c, ["BAD", "OK"], 100_000, fundamentals_map=fm)
        assert out == ["OK"]

    def test_f7b_missing_fundamentals_passes(self):
        c = FakeBrokerClient()
        c.stock_trades = {"UNK": 50.0}
        out = filter_underlying(c, ["UNK"], 100_000, fundamentals_map={"OTHER": {"blocked": True}})
        assert out == ["UNK"]

    def test_f8_bp_drop(self):
        c = FakeBrokerClient()
        c.stock_trades = {"BIG": 800.0, "SMALL": 50.0}
        out = filter_underlying(c, ["BIG", "SMALL"], 70_000)
        assert out == ["SMALL"]

    def test_f9_high_iv_does_not_remove(self):
        c = FakeBrokerClient()
        c.stock_trades = {"AAA": 50.0}
        vm = {"AAA": {"iv_rank": 90, "rv_20d": 0.5}}
        assert filter_underlying(c, ["AAA"], 100_000, vol_map=vm) == ["AAA"]

    def test_f10_dividend_ignored_for_puts(self):
        c = FakeBrokerClient()
        c.stock_trades = {"AAA": 50.0}
        dm = {"AAA": date.today()}
        assert filter_underlying(c, ["AAA"], 100_000, dividend_map=dm, is_call=False) == ["AAA"]
        assert filter_underlying(c, ["AAA"], 100_000, dividend_map=dm, is_call=True) == []


class TestSellCalls:
    def _call_client(self, underlying="F", purchase=13.0):
        c = FakeBrokerClient()
        exp = date.today() + timedelta(days=30)
        raw, snap = make_put(underlying, purchase + 2.0, dte=30, bid=0.5,
                             ask=0.54, delta=0.25, oi=500)
        # flip to call symbol
        raw.symbol = raw.symbol.replace("P0", "C0", 1) if False else make_occ(underlying, exp, "C", purchase + 2.0)
        c.option_chain[underlying] = [(raw, snap)]
        c.stock_trades[underlying] = purchase + 1.0
        return c

    def test_sc1_under_100_no_raise(self):
        c = self._call_client()
        sell_calls(c, "F", 13.0, 99)
        assert not c.submitted

    def test_sc2_dividend_block(self):
        c = self._call_client()
        dm = {"F": date.today() + timedelta(days=1)}
        sell_calls(c, "F", 13.0, 100, dividend_map=dm)
        assert not c.option_sell_attempts

    def test_sc3_normal_path_sells(self):
        c = self._call_client()
        sell_calls(c, "F", 13.0, 200,
                   execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert c.option_sells

    def test_sc4_no_calls_clean(self):
        c = FakeBrokerClient()
        c.stock_trades = {"F": 14.0}
        sell_calls(c, "F", 13.0, 200,
                   execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert not c.submitted


class TestSgovSweep:
    """SG: sync_sgov_real sweep math — both directions market, no dupes."""

    def _setup(self, cash, sgov_qty, stock_bp, sgov_price=100.50):
        from scripts.run_strategy import sync_sgov_real
        c = FakeBrokerClient(FakeAccount(cash=cash, equity=cash + sgov_qty * sgov_price,
                                         buying_power=stock_bp))
        if sgov_qty:
            c.positions.append(FakePosition("SGOV", sgov_qty, sgov_price, sgov_price))
        c.stock_trades["SGOV"] = sgov_price
        return sync_sgov_real, c

    def test_sg1_under_target_buys_diff(self):
        fn, c = self._setup(cash=10_000, sgov_qty=0, stock_bp=50_000)
        fn(c, logging.getLogger("t"), risk_override=0)
        # target = min(10000-500, 50000-1000+0) = 9500 -> 94 shares
        assert c.stock_buys == [("SGOV", 94)]

    def test_sg2_over_target_sells_diff(self):
        fn, c = self._setup(cash=100, sgov_qty=500, stock_bp=1_000_000)
        # total liquid = 100 + 50250 = 50350; target = 49850 -> floor(49850/100.50)=496 shares; sell 4
        fn(c, logging.getLogger("t"), risk_override=0)
        assert c.stock_sells == [("SGOV", 4)]

    def test_sg3_at_target_no_order(self):
        fn, c = self._setup(cash=500, sgov_qty=100, stock_bp=1_000_000)
        # liquid = 500 + 10050 = 10550; target ideal = 10050 -> exactly 100 shares
        fn(c, logging.getLogger("t"), risk_override=0)
        assert not c.stock_buys and not c.stock_sells

    def test_sg4_open_buy_skips_duplicate(self):
        fn, c = self._setup(cash=10_000, sgov_qty=0, stock_bp=50_000)
        from tests.stress.fakes import FakeOrder
        o = FakeOrder("SGOV", 5, "buy", status="new")
        c.orders[o.id] = o
        fn(c, logging.getLogger("t"), risk_override=0)
        assert not c.stock_buys, "open SGOV buy must suppress the sweep buy"

    def test_sg5_missing_quote_falls_back(self):
        fn, c = self._setup(cash=500, sgov_qty=100, stock_bp=1_000_000)
        c.stock_trades = {}  # no quote at all
        fn(c, logging.getLogger("t"), risk_override=0)  # must not raise

    def test_sg6_market_both_ways(self):
        fn, c = self._setup(cash=10_000, sgov_qty=0, stock_bp=50_000)
        fn(c, logging.getLogger("t"), risk_override=0)
        assert all(o.type == "market" for o in c.submitted)

    def test_sg7_pending_sell_suppresses_double_sell(self):
        # 2026-08-18 midday run: a 416-share funding-queue pre-fund sale was
        # pending, then the sweep tried to sell 541 MORE against only 196
        # available -> Alpaca 403 "insufficient qty". Pending SGOV sells must
        # count against the position when computing the sweep diff.
        fn, c = self._setup(cash=100, sgov_qty=500, stock_bp=1_000_000)
        # liquid = 100 + 50250 = 50350; target = 49850 -> 496 shares; naive diff -4
        from tests.stress.fakes import FakeOrder
        o = FakeOrder("SGOV", 100, "sell", status="new")
        c.orders[o.id] = o
        fn(c, logging.getLogger("t"), risk_override=0)
        assert not c.stock_sells, "pending sell already covers the diff; sweep must not sell again"
        assert not c.stock_buys

    def test_sg8_pending_sell_caps_additional_sell(self):
        # Same root cause, other branch: target below (qty - pending) still
        # sells, but only the remainder, never more than will remain.
        fn, c = self._setup(cash=10_000, sgov_qty=500, stock_bp=1_000_000)
        # queue reserve -> target 300 shares: liquid 60250 - 500 - 29600 = 30150 -> 300
        import json, os
        from datetime import date as _date, timedelta as _td
        qpath = os.environ["WHEEL_FUNDING_QUEUE"]
        tomorrow = (_date.today() + _td(days=1)).isoformat()
        # reserve = need - opt_bp(14000) -> need 43600 for a 29600 reserve
        with open(qpath, "w") as f:
            json.dump({"entries": [{"symbol": "X260918P00436000", "underlying": "X",
                                    "strike": 436.0, "expiration": "2026-09-18",
                                    "need": 43_600, "score": 0.01,
                                    "queued_at": "2026-08-18T00:00:00-04:00",
                                    "valid_for": tomorrow}],
                       "prefunded": 0.0}, f)
        from tests.stress.fakes import FakeOrder
        o = FakeOrder("SGOV", 100, "sell", status="new")
        c.orders[o.id] = o
        fn(c, logging.getLogger("t"), risk_override=0)
        # naive diff = 300 - 500 = -200; pending 100 -> effective 400 -> sell exactly 100
        assert c.stock_sells == [("SGOV", 100)]

    def test_sg9_pending_sell_suppresses_buy_churn(self):
        # Position is mid-flight down (pending sell); a target ABOVE the
        # effective qty must NOT trigger a buy-back — that was the Aug 17
        # sell+buy-back churn the funding queue exists to eliminate.
        fn, c = self._setup(cash=10_000, sgov_qty=500, stock_bp=1_000_000)
        # liquid 60250; target 59750 -> 594 shares; naive diff +94 (buy)
        from tests.stress.fakes import FakeOrder
        o = FakeOrder("SGOV", 100, "sell", status="new")
        c.orders[o.id] = o
        fn(c, logging.getLogger("t"), risk_override=0)
        assert not c.stock_buys and not c.stock_sells

    def test_sg10_low_stock_bp_never_forces_sell(self):
        # 2026-08-21: with stock BP under the $1k buffer the old cap
        # (max(0, stock_bp-1000) + sgov_mv) forced a sale of (1000-stock_bp)
        # dollars BELOW current holdings — 10 shares sold in the morning run,
        # 6 more midday, then a 1-share buy-back in the afternoon. Buying
        # power constrains PURCHASES only; holding SGOV consumes none. Low BP
        # must mean "no buys", never "forced sell".
        fn, c = self._setup(cash=40_000, sgov_qty=616, stock_bp=0)
        # liquid = 40000 + 616*100.50 = 101908; ideal target = 101408 -> 1009
        # shares (buy side), real target capped at holdings (no buy capacity):
        # 61908 -> exactly 616 shares -> no order either way.
        fn(c, logging.getLogger("t"), risk_override=0)
        assert not c.stock_sells and not c.stock_buys

    def test_sg11_filled_prefund_sale_suppresses_double_sell(self):
        # 2026-08-21 morning run: the funding-queue pre-fund market sale
        # FILLED instantly, so the open-orders guard couldn't see it, and
        # Alpaca's position endpoint still showed the pre-sale qty — the
        # sweep sold the same 10 shares again 29s later. The pre-fund path
        # now records its qty in the queue ledger; the sweep must subtract
        # it even with no open order visible.
        import json, os
        from datetime import datetime as _dt
        fn, c = self._setup(cash=4_495, sgov_qty=626, stock_bp=0, sgov_price=100.59)
        qpath = os.environ["WHEEL_FUNDING_QUEUE"]
        with open(qpath, "w") as f:
            json.dump({"entries": [{"symbol": "X261016P00190000", "underlying": "X",
                                    "strike": 190.0, "expiration": "2026-10-16",
                                    "need": 19_000.0, "score": 0.05,
                                    "queued_at": _dt.now().isoformat(timespec="seconds"),
                                    "valid_for": _dt.now().date().isoformat()}],
                       "prefunded": 5_000.0,
                       "last_prefund": {"qty": 10,
                                        "at": _dt.now().astimezone().isoformat(timespec="seconds")}}, f)
        # reserve = 19000 - opt_bp(14000) = 5000; liquid = 4495 + 626*100.59
        # = 67464; ideal target = 67464 - 500 - 5000 = 61964 -> 616 shares.
        # Naive diff vs the stale 626 = -10 (would double-sell the pre-fund's
        # 10); pending-prefund adjustment -> effective 616 -> no order.
        fn(c, logging.getLogger("t"), risk_override=0)
        assert not c.stock_sells and not c.stock_buys


class TestPaperDiscipline:
    def test_p1_is_paper_true(self):
        from config.credentials import IS_PAPER
        assert IS_PAPER is True

    def test_p2_no_paper_false_construction(self):
        import subprocess
        out = subprocess.run(
            ["grep", "-rn", "paper=" + "False", "--include=*.py",
             "core", "scripts", "config", "models", "app_logging"],
            capture_output=True, text=True,
            cwd=__file__.rsplit("/tests/", 1)[0])
        assert out.stdout.strip() == "", f"paper=False found: {out.stdout}"

    def test_p3_paper_defaults_true_everywhere(self):
        import subprocess
        repo = __file__.rsplit("/tests/", 1)[0]
        out = subprocess.run(
            ["grep", "-rn", "paper" + "=", "--include=*.py", "core", "scripts", "config"],
            capture_output=True, text=True, cwd=repo)
        for line in out.stdout.strip().splitlines():
            # allowed: default True, or driven by the IS_PAPER credential
            assert ("paper=True" in line or "paper=paper" in line
                    or "paper=IS_PAPER" in line), f"suspicious paper flag: {line}"
