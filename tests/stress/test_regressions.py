"""Stress tests: regression coverage for this week's real production bugs."""
import json
from datetime import date, timedelta

from core.strategy import filter_underlying
from core.execution import sell_puts, sell_calls, _prefund_queue_with_sgov
from core.funding_queue import FundingQueue
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
    """R4 (rewritten 2026-08-17): T+1 queue-based funding replaces same-day
    SGOV funding. Same-day sales never freed options BP in time (settled cash
    only), so the candidate was skipped and the sweep bought the SGOV right
    back — the Aug 17 15:05 ET run churned ~$165k this way. New contract:
    skip the CSP, queue it for next-day funding, ONE pre-fund sale per run."""

    def _rich_client(self, opt_bp, sgov_qty, sgov_price=100.50):
        c = _client_with_puts([("AMD", 440, 10.0, -0.25)], opt_bp=opt_bp)
        if sgov_qty:
            c.add_sgov(sgov_qty, sgov_price)
        return c

    def test_r4a_underfunded_candidate_queued_never_sold_same_day(self):
        c = self._rich_client(opt_bp=13_000, sgov_qty=500)
        c.sgov_sale_credits_bp = True  # even if BP *did* move same-day, we no longer try
        sell_puts(c, ["AMD"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=True)
        assert not c.option_sells, "T+1: candidate must wait for next-day settled cash"
        assert len(c.stock_sells) == 1, "exactly one pre-fund SGOV sale for the queue"
        sym, qty = c.stock_sells[0]
        assert sym == "SGOV"
        # deficit = 44000-13000 = 31000; +150 buffer; /100.50 -> ceil = 310
        assert qty == 310
        q = FundingQueue().load()
        assert len(q.entries) == 1 and q.entries[0]["underlying"] == "AMD"
        assert q.prefunded > 0

    def test_r4b_multiple_candidates_single_prefund_sale(self):
        c = _client_with_puts([("AMD", 440, 10.0, -0.25), ("INTC", 90, 2.0, -0.25)],
                              opt_bp=2_000)
        c.add_sgov(600)
        sell_puts(c, ["AMD", "INTC"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=True)
        assert not c.option_sells
        assert len(c.stock_sells) == 1, "the churn bug was one sale per candidate; must be one per run"
        q = FundingQueue().load()
        assert len(q.entries) == 2
        assert q.prefunded >= 51_000  # 44000 + 9000 - 2000 BP

    def test_r4c_sgov_sale_throws_queue_survives_no_crash(self):
        c = self._rich_client(opt_bp=13_000, sgov_qty=500)
        c.raise_on_stock_sell = Exception("market data unavailable")
        sell_puts(c, ["AMD"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=True)
        assert not c.option_sells
        q = FundingQueue().load()
        assert len(q.entries) == 1 and q.prefunded == 0, "failed sale must not be credited as prefunded"

    def test_r4d_no_sgov_held_queues_without_sale(self):
        c = self._rich_client(opt_bp=13_000, sgov_qty=0)
        sell_puts(c, ["AMD"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=True)
        assert not c.option_sells and not c.stock_sells
        assert len(FundingQueue().load().entries) == 1

    def test_r4_queue_capped_at_risk_headroom(self):
        # 2026-08-18 review: 6 entries totalling $134,750 queued against
        # ~$41,650 of real risk headroom, draining SGOV to zero. Total queued
        # need must never exceed remaining risk-cap headroom.
        c = _client_with_puts([("AAA", 400, 10.0, -0.25), ("BBB", 400, 10.0, -0.25)],
                              opt_bp=2_000)
        c.add_sgov(600)
        sell_puts(c, ["AAA", "BBB"], 50_000,  # only $50k risk headroom
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=True)
        assert not c.option_sells
        q = FundingQueue().load()
        assert len(q.entries) == 1, "second $40k candidate would push queued need past $50k headroom"
        assert q.pending_need() <= 50_000

    def test_r4_queue_cap_zero_headroom_nothing_queued(self):
        c = _client_with_puts([("AAA", 400, 10.0, -0.25)], opt_bp=2_000)
        c.add_sgov(600)
        sell_puts(c, ["AAA"], 0,  # no risk headroom at all
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=True)
        assert not c.option_sells and not c.stock_sells
        assert len(FundingQueue().load().entries) == 0

    def test_r4e_prefund_caps_at_holdings(self):
        c = self._rich_client(opt_bp=13_900, sgov_qty=100)
        q = FundingQueue().load()
        q.add("AMD260918P00440000", "AMD", 440.0, "2026-09-18", 44_000)
        sold = _prefund_queue_with_sgov(c, deficit=30_100, risk_bp=500_000, queue=q)
        sym, qty = c.stock_sells[0]
        assert qty == 100, "must cap at SGOV holdings"
        assert sold == 100 * 100.50

    def test_r4f_risk_cap_bounds_prefund(self):
        c = self._rich_client(opt_bp=13_000, sgov_qty=500)
        q = FundingQueue().load()
        q.add("AMD260918P00440000", "AMD", 440.0, "2026-09-18", 44_000)
        _prefund_queue_with_sgov(c, deficit=31_000, risk_bp=20_000, queue=q)
        sym, qty = c.stock_sells[0]
        # capped at risk cap 20000 -> ceil(20150/100.50) = 201
        assert qty == 201, "pre-fund must never exceed the risk cap"
        q2 = FundingQueue().load()
        q2.add("X", "X", 1.0, None, 1.0)
        sold = _prefund_queue_with_sgov(c, deficit=10.0, risk_bp=0, queue=q2)
        assert sold == 0 and len(c.stock_sells) == 1, "zero risk cap -> no sale"

    def test_r4g_second_run_same_day_no_double_sell(self):
        c = self._rich_client(opt_bp=13_000, sgov_qty=900)
        for _ in range(2):  # midday + afternoon runs, same queue file
            sell_puts(c, ["AMD"], 500_000,
                      execution_config={"limit_enabled": False, "wait_seconds": 0},
                      fund_with_sgov=True)
        assert len(c.stock_sells) == 1, "prefund ledger must stop a second same-day sale for the same entries"

    def test_r4h_next_day_settled_cash_fills_and_clears_queue(self):
        c = self._rich_client(opt_bp=13_000, sgov_qty=500)
        sell_puts(c, ["AMD"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=True)
        assert not c.option_sells
        # Next morning: sale settled -> options BP now covers the candidate
        c.account.options_buying_power = 100_000
        sell_puts(c, ["AMD"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=True)
        assert any("AMD" in s for s in c.option_sells), "settled cash must fund the queued candidate"
        assert len(c.stock_sells) == 1, "no new SGOV sale once BP covers"
        assert not FundingQueue().load().entries, "filled candidate must leave the queue"

    def test_r5_journal_survives_date_typed_dividend_ex(self, tmp_path):
        # 2026-08-18: KO's 59% profit-take close was silently dropped from
        # wheel_trades.jsonl — dividend_ex arrived as a datetime.date (KO
        # ex-div 2026-09-15), json.dumps raised, and the whole entry landed
        # in logging_errors instead. VZ's identical close the day before
        # survived only because VZ had dividend_ex=None.
        from datetime import date as _date
        from app_logging.strategy_logger import StrategyLogger
        sl = StrategyLogger(enabled=True,
                            log_path=str(tmp_path / "strategy_log.json"),
                            jsonl_path=str(tmp_path / "wheel_trades.jsonl"))
        sl.log_detailed_trade(
            {"underlying": "KO", "symbol": "KO260918P00082500", "strike": 82.5,
             "dte": 31, "delta": -0.11, "bid_price": 0.28, "ask_price": 0.30,
             "oi": None, "contract_type": "put", "underlying_price": 88.5,
             "dividend_ex": _date(2026, 9, 15),
             "profit_dollars_gross": 40.0, "profit_dollars_net": 40.0},
            score=40.0, decision_type="close_profit_take_50")
        lines = (tmp_path / "wheel_trades.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1, "close entry must be journaled even with a date-typed dividend_ex"
        entry = json.loads(lines[0])
        assert entry["trade_type"] == "close_profit_take_50"
        assert entry["contract"]["dividend_ex"] == "2026-09-15"
        assert not sl.log_entry.get("logging_errors"), "no silent logging failures"

    def test_r4j_queue_replaces_stale_same_underlying(self):
        # 2026-08-18: three runs queued three DIFFERENT AAPL contracts
        # (fresh strike/expiry each run) — dedupe by OCC symbol let the
        # queue hold $87k of AAPL need against ~$42k risk headroom, and the
        # sweep reserved $131,962 (> account equity), draining SGOV to 0.
        # Only one contract per underlying can ever fill, so a fresh entry
        # must replace stale same-underlying ones.
        q = FundingQueue(today=date.today())
        q.add("AAPL261016P00285000", "AAPL", 285.0, "2026-10-16", 28_500, 0.030)
        q.add("JNJ261016P00250000", "JNJ", 250.0, "2026-10-16", 25_000, 0.041)
        assert q.add("AAPL260911P00295000", "AAPL", 295.0, "2026-09-11", 29_500, 0.052)
        aapl = [e for e in q.entries if e["underlying"] == "AAPL"]
        assert len(aapl) == 1 and aapl[0]["symbol"] == "AAPL260911P00295000"
        assert q.pending_need() == 29_500 + 25_000
        # exact same-symbol requeue is still a no-op
        assert not q.add("AAPL260911P00295000", "AAPL", 295.0, "2026-09-11", 29_500, 0.052)
        # prefunded dollars stay credited: already-sold cash is fungible
        q2 = FundingQueue(today=date.today())
        q2.prefunded = 81_000
        q2.add("OLD1", "AAPL", 285.0, "2026-10-16", 28_500)
        q2.add("NEW1", "AAPL", 295.0, "2026-09-11", 29_500)
        assert q2.prefunded == 81_000, "replacement must not touch the prefunded ledger"

    def test_r4i_queue_entries_expire_after_one_trading_day(self):
        from datetime import date as _date, timedelta as _td
        today = _date.today()
        stale = today - _td(days=2)
        q = FundingQueue(today=today)
        q.add("OLD260918P00440000", "OLD", 440.0, "2026-09-18", 44_000)
        q.entries[0]["valid_for"] = (today - _td(days=1)).isoformat()  # valid yesterday only
        q.prefunded = 44_000
        q.dirty = True
        q.save()
        q2 = FundingQueue(today=today).load()
        dropped = q2.expire()
        assert len(dropped) == 1 and not q2.entries
        assert q2.prefunded == 0, "expiring an entry must release its prefunded cash back to the sweep"

    def test_r4j_corrupt_queue_disables_prefunding(self):
        from core.funding_queue import queue_path
        queue_path().write_text("{not json")
        c = self._rich_client(opt_bp=13_000, sgov_qty=500)
        sell_puts(c, ["AMD"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=True)
        assert not c.stock_sells, "corrupt queue state must fail safe: no SGOV sale off unknown state"

    def test_r4k_funding_disabled_still_queues_but_never_sells(self):
        c = self._rich_client(opt_bp=13_000, sgov_qty=500)
        sell_puts(c, ["AMD"], 500_000,
                  execution_config={"limit_enabled": False, "wait_seconds": 0},
                  fund_with_sgov=False)
        assert not c.option_sells and not c.stock_sells
        assert len(FundingQueue().load().entries) == 1


class TestFundingQueueMath:
    """The SGOV sweep in run_strategy reserves cash via reserve_amount; the
    pre-fund sale sizes itself via prefund_deficit. Both must be exact."""

    def test_reserve_is_pending_minus_settled_bp(self):
        q = FundingQueue()
        q.add("A", "A", 100.0, "2026-09-18", 10_000)
        q.add("B", "B", 200.0, "2026-09-18", 20_000)
        assert q.reserve_amount(opt_bp=25_000) == 5_000
        assert q.reserve_amount(opt_bp=30_000) == 0
        assert q.reserve_amount(opt_bp=None) == 30_000

    def test_prefund_deficit_subtracts_bp_and_prefunded(self):
        q = FundingQueue()
        q.add("A", "A", 100.0, "2026-09-18", 10_000)
        q.record_prefund(4_000)
        assert q.prefund_deficit(opt_bp=3_000) == 3_000
        assert q.prefund_deficit(opt_bp=6_500) == 0, "settled BP + prefunded covers -> never sell"

    def test_mark_filled_releases_prefund(self):
        q = FundingQueue()
        q.add("A", "A", 100.0, "2026-09-18", 10_000)
        q.add("B", "B", 200.0, "2026-09-18", 20_000)
        q.record_prefund(30_000)
        q.mark_filled("A")
        assert q.prefunded == 20_000 and len(q.entries) == 1
        assert q.mark_filled("NOPE") is False

    def test_load_dedupes_legacy_same_underlying_entries(self):
        # 2026-08-19: state written before the add()-time replacement fix
        # held 3 AAPL entries ($87k of a $134k queue) with no fresh AAPL add
        # to trigger cleanup — reserve pinned the SGOV sweep at 0 all day.
        from core.funding_queue import queue_path
        entries = [
            {"symbol": "AAPL261016P00285000", "underlying": "AAPL", "strike": 285.0,
             "expiration": "2026-10-16", "need": 28_500, "score": 0.03,
             "queued_at": "2026-08-18T14:08:06+00:00", "valid_for": "2099-01-01"},
            {"symbol": "AAPL260911P00295000", "underlying": "AAPL", "strike": 295.0,
             "expiration": "2026-09-11", "need": 29_500, "score": 0.05,
             "queued_at": "2026-08-18T19:07:59+00:00", "valid_for": "2099-01-01"},
            {"symbol": "KO260918P00085000", "underlying": "KO", "strike": 85.0,
             "expiration": "2026-09-18", "need": 8_500, "score": 0.03,
             "queued_at": "2026-08-18T19:07:59+00:00", "valid_for": "2099-01-01"},
        ]
        queue_path().write_text(json.dumps({"entries": entries, "prefunded": 81_465.75}))
        q = FundingQueue().load()
        aapl = [e for e in q.entries if e["underlying"] == "AAPL"]
        assert len(q.entries) == 2, "load() must drop stale same-underlying duplicates"
        assert len(aapl) == 1 and aapl[0]["symbol"] == "AAPL260911P00295000", "newest entry wins"
        assert q.prefunded == 81_465.75, "dedupe must not touch the prefunded ledger (mirrors add())"
        assert q.dirty, "dedupe must persist on next save()"

    def test_mark_filled_drops_same_underlying_entries(self):
        # 2026-08-19: AAPL260911P00300000 filled while AAPL260911P00295000 sat
        # queued — exact-symbol match missed, $29.5k kept reserving for a name
        # that now has an open CSP.
        q = FundingQueue()
        # legacy pre-fix state: two AAPL entries coexist (add() now prevents this)
        q.entries = [
            {"symbol": "AAPL260911P00295000", "underlying": "AAPL", "need": 29_500},
            {"symbol": "AAPL261016P00285000", "underlying": "AAPL", "need": 28_500},
            {"symbol": "KO260918P00085000", "underlying": "KO", "need": 8_500},
        ]
        q.record_prefund(66_500)
        assert q.mark_filled("AAPL260911P00300000", underlying="AAPL") is True
        assert [e["underlying"] for e in q.entries] == ["KO"]
        assert q.prefunded == 8_500, "dropped AAPL entries' prefunded dollars are consumed by the fill"

    def test_mark_filled_without_underlying_keeps_exact_match_only(self):
        q = FundingQueue()
        q.add("A", "AAPL", 100.0, "2026-09-18", 10_000)
        assert q.mark_filled("OTHER") is False
        assert len(q.entries) == 1

    def test_next_trading_day_skips_weekend(self):
        from core.funding_queue import next_trading_day
        from datetime import date as _d
        assert next_trading_day(_d(2026, 8, 14)) == _d(2026, 8, 17)  # Fri -> Mon
        assert next_trading_day(_d(2026, 8, 17)) == _d(2026, 8, 18)  # Mon -> Tue


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
