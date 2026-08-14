"""Stress tests: regression coverage for this week's real production bugs."""
from datetime import date, timedelta

from core.strategy import filter_underlying
from core.execution import sell_puts, sell_calls, _fund_csp_with_sgov
from tests.stress.fakes import FakeBrokerClient, FakeAccount, make_put


def _client_with_puts(specs, opt_bp=100_000.0):
    """specs: list of (underlying, strike, bid, delta)."""
    c = FakeBrokerClient(FakeAccount(options_buying_power=opt_bp))
    for u, k, bid, delta in specs:
        raw, snap = make_put(u, k, dte=30, bid=bid, ask=bid + 0.04, delta=delta, oi=500)
        c.option_chain.setdefault(u, []).append((raw, snap))
        c.stock_trades[u] = k * 1.10
    return c


class TestLiquidityRegression:
    """R1: 3c09d82 — liquidity block built safe=[] and never appended."""

    def test_symbols_survive_populated_liquidity_map(self):
        c = FakeBrokerClient()
        c.stock_trades = {"AAA": 50.0, "BBB": 60.0, "CCC": 70.0}
        liq = {s: {"trend_ok": True, "avg_5d": 5_000_000} for s in ("AAA", "BBB", "CCC")}
        out = filter_underlying(c, ["AAA", "BBB", "CCC"], 100_000, liquidity_map=liq)
        assert sorted(out) == ["AAA", "BBB", "CCC"], "symbols vanished under populated liquidity map (3c09d82 regression)"

    def test_thin_drying_symbol_dropped(self):
        c = FakeBrokerClient()
        c.stock_trades = {"AAA": 50.0, "THIN": 60.0}
        liq = {"AAA": {"trend_ok": True, "avg_5d": 5_000_000},
               "THIN": {"trend_ok": False, "avg_5d": 100_000, "reason": "drying"}}
        out = filter_underlying(c, ["AAA", "THIN"], 100_000, liquidity_map=liq)
        assert out == ["AAA"]

    def test_drying_but_liquid_symbol_kept(self):
        c = FakeBrokerClient()
        c.stock_trades = {"AAA": 50.0}
        liq = {"AAA": {"trend_ok": False, "avg_5d": 36_000_000, "reason": "drying"}}
        out = filter_underlying(c, ["AAA"], 100_000, liquidity_map=liq)
        assert out == ["AAA"], "avg_5d >= 300k must not be dropped even when trend drying"


class TestInsufficientBpBreak:
    """R2: 50a0793 — loop must break on insufficient-BP rejection."""

    def test_break_on_insufficient_bp(self):
        c = _client_with_puts([("AAA", 50, 1.0, -0.25), ("BBB", 40, 1.0, -0.25)],
                              opt_bp=100_000)
        c.raise_on_option_sell = Exception('{"message":"insufficient options buying power for cash-secured put"}')
        sell_puts(c, ["AAA", "BBB"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert len(c.option_sell_attempts) == 1, "loop must stop after first insufficient-BP rejection"

    def test_non_bp_error_continues(self):
        calls = {"n": 0}

        class Flaky(FakeBrokerClient):
            def market_sell(self, symbol, qty=1):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise Exception("timeout waiting for quote")
                return super().market_sell(symbol, qty)

        c = _client_with_puts([("AAA", 50, 1.0, -0.25), ("BBB", 40, 1.0, -0.25)],
                              opt_bp=100_000)
        c.__class__ = Flaky
        sell_puts(c, ["AAA", "BBB"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert calls["n"] == 2 and len(c.option_sells) == 1


class TestOptionsBpSkip:
    """R3: 75795d5 — over-BP candidates skipped, cheaper ones still tried."""

    def test_expensive_skipped_cheap_fills(self):
        c = _client_with_puts([("EXP", 440, 10.0, -0.25), ("CHP", 62, 0.30, -0.25)],
                              opt_bp=13_000)
        # make EXP score higher by giving it the bigger premium
        sell_puts(c, ["EXP", "CHP"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=False)
        assert any("CHP" in s for s in c.option_sells)
        assert not any("EXP" in s for s in c.option_sells), "over-options-BP candidate must be skipped, not attempted"

    def test_all_over_bp_no_orders(self):
        c = _client_with_puts([("EXP", 440, 10.0, -0.25), ("EXP2", 300, 8.0, -0.25)],
                              opt_bp=13_000)
        sell_puts(c, ["EXP", "EXP2"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=False)
        assert not c.option_sells


class TestSgovFunding:
    """R4: 5115019 — SGOV same-day funding of CSPs."""

    def _rich_client(self, opt_bp, sgov_qty, sgov_price=100.50):
        c = _client_with_puts([("AMD", 440, 10.0, -0.25)], opt_bp=opt_bp)
        if sgov_qty:
            c.add_sgov(sgov_qty, sgov_price)
        return c

    def test_r4a_sale_fills_put_proceeds(self):
        c = self._rich_client(opt_bp=13_000, sgov_qty=500)
        c.sgov_sale_credits_bp = True  # emulate Alpaca: filled SGOV sale frees options BP
        sell_puts(c, ["AMD"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=True)
        sym, qty = c.stock_sells[0]
        assert sym == "SGOV"
        # deficit = 44000-13000 = 31000; +150 buffer; /100.50 -> ceil = 310
        assert qty == 310
        assert any("AMD" in s for s in c.option_sells), "funded candidate must proceed to the put sale"

    def test_r4b_bp_still_short_skips_cleanly(self):
        c = self._rich_client(opt_bp=13_000, sgov_qty=500)
        sell_puts(c, ["AMD"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=True)
        assert not c.option_sells, "fake BP never rises -> put must NOT be sold"

    def test_r4c_sgov_sale_throws_no_put(self):
        c = self._rich_client(opt_bp=13_000, sgov_qty=500)
        c.raise_on_stock_sell = Exception("market data unavailable")
        sell_puts(c, ["AMD"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=True)
        assert not c.option_sells

    def test_r4d_no_sgov_held_skips(self):
        c = self._rich_client(opt_bp=13_000, sgov_qty=0)
        sell_puts(c, ["AMD"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=True)
        assert not c.option_sells and not c.stock_sells

    def test_r4e_deficit_rounds_up_and_caps_at_holdings(self):
        c = self._rich_client(opt_bp=13_900, sgov_qty=100)  # deficit 30,100 -> 301 shares but only 100 held
        _fund_csp_with_sgov(c, need=44_000, opt_bp=13_900, risk_bp=500_000)
        sym, qty = c.stock_sells[0]
        assert qty == 100, "must cap at SGOV holdings"

    def test_r4f_risk_cap_short_never_attempts_sale(self):
        c = self._rich_client(opt_bp=13_000, sgov_qty=500)
        ok = _fund_csp_with_sgov(c, need=44_000, opt_bp=13_000, risk_bp=20_000)
        assert ok is False and not c.stock_sells, "risk cap shortfall must block the SGOV sale"

    def test_r4g_pending_fill_skips_without_put(self):
        c = self._rich_client(opt_bp=13_000, sgov_qty=500)
        c.auto_fill = False  # market closed: order stays accepted
        # shorten the fill wait so the test is fast
        import core.execution as ex
        orig_sleep = ex.time.sleep
        ex.time.sleep = lambda *_: None
        try:
            sell_puts(c, ["AMD"], 500_000,
                      execution_config={"limit_enabled": False, "wait_seconds": 0},
                      fund_with_sgov=True)
        finally:
            ex.time.sleep = orig_sleep
        assert not c.option_sells, "pending SGOV fill -> put must not be sold"


class TestOptionableSyncIsolation:
    """R5: optionable push failure must not kill the loop."""

    def test_sync_failure_continues(self, monkeypatch):
        import core.execution as ex
        monkeypatch.setattr(ex, "push_trade_to_optionable",
                            lambda *a, **k: (_ for _ in ()).throw(Exception("connection refused")))
        c = _client_with_puts([("AAA", 50, 1.0, -0.25), ("BBB", 40, 1.0, -0.25)],
                              opt_bp=100_000)
        sell_puts(c, ["AAA", "BBB"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0})
        assert len(c.option_sells) == 2


class TestSellCallsGuard:
    """R6: <100 shares must log and return, never raise."""

    def test_under_100_shares_no_raise(self):
        c = FakeBrokerClient()
        sell_calls(c, "F", purchase_price=13.0, stock_qty=50)
        assert not c.submitted
