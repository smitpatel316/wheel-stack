"""Fuzz stress: seeded random degenerate inputs through the engine paths.

Goal: prove no exception escapes from filter_options, score_options,
select_options, sell_puts, evaluate_close_need, evaluate_roll_need,
find_roll_targets for arbitrary (including pathological) market data.
"""
import math
import random
from datetime import date, timedelta

import pytest

from core.strategy import filter_options, score_options, select_options
from core.execution import sell_puts
from core.roller import evaluate_roll_need, find_roll_targets, RollCandidate
from core.closer import evaluate_close_need
from models.contract import Contract
from tests.stress.fakes import FakeBrokerClient, FakeAccount, make_put, make_occ

DEGEN = [None, 0, 0.0, -1.0, -0.0001, 1e-9, 1e9, float("nan"), float("inf"),
         -float("inf"), 0.5, 100.0]


def _rand_contract(rng, underlying="FZZ"):
    exp = date.today() + timedelta(days=rng.choice([0, 1, 3, 30, 400]))
    return Contract(
        symbol=make_occ(underlying, exp, "P", 50.0), underlying=underlying,
        strike=rng.choice(DEGEN + [50.0]), dte=rng.choice(DEGEN + [30]),
        bid_price=rng.choice(DEGEN + [1.0]), ask_price=rng.choice(DEGEN + [1.1]),
        delta=rng.choice(DEGEN + [-0.25]), oi=rng.choice(DEGEN + [500]),
    )


def _rand_candidate(rng):
    exp = date.today() + timedelta(days=rng.choice([-1, 0, 1, 2, 5, 20, 60]))
    strike = rng.choice([1.0, 50.0, 1e6])
    und = rng.choice([0.0, 1e-9, 49.0, 50.0, 60.0, 1e6])
    return RollCandidate(
        symbol=make_occ("FZZ", exp, "P", strike), underlying="FZZ",
        strike=strike, expiration=exp, dte=rng.choice([-5, 0, 1, 3, 20, 400]),
        qty=rng.choice([-1, -10, 0]), avg_entry_price=rng.choice([0.0, 1.0, 100.0]),
        current_price=rng.choice(DEGEN + [0.5]), underlying_price=und,
        delta=rng.choice(DEGEN + [-0.3]), bid=rng.choice(DEGEN + [0.5]),
        ask=rng.choice(DEGEN + [0.55]), is_put=rng.choice([True, False]),
        itm_pct=0.0, loss_pct=0.0, profit_pct=0.0)


class TestFuzz:
    def test_filter_options_never_raises(self):
        rng = random.Random(42)
        for _ in range(300):
            opts = [_rand_contract(rng) for _ in range(rng.randint(0, 12))]
            filter_options(opts)  # must not raise

    def test_score_select_never_raises(self):
        rng = random.Random(7)
        for _ in range(200):
            opts = [_rand_contract(rng) for _ in range(rng.randint(1, 8))]
            for o in opts:
                o.strike = o.strike if isinstance(o.strike, (int, float)) else 50.0
                o.delta = o.delta if isinstance(o.delta, (int, float)) else -0.2
                o.bid_price = o.bid_price if isinstance(o.bid_price, (int, float)) else 1.0
                o.dte = o.dte if isinstance(o.dte, (int, float)) else 30
            scores = score_options(opts)  # returns parallel scores list
            select_options(opts, scores, n=rng.randint(0, 3))

    def test_close_roll_decisions_never_raise(self):
        rng = random.Random(99)
        for _ in range(300):
            cand = _rand_candidate(rng)
            d1 = evaluate_close_need(cand)
            d2 = evaluate_roll_need(cand)
            targets = [_rand_contract(rng) for _ in range(rng.randint(0, 5))]
            for t in targets:
                if not isinstance(t.strike, (int, float)) or t.strike is None:
                    t.strike = 50.0
                if not isinstance(t.dte, (int, float)) or t.dte is None:
                    t.dte = 30
                if not isinstance(t.bid_price, (int, float)) or t.bid_price is None:
                    t.bid_price = 1.0
                if not isinstance(t.delta, (int, float)) or t.delta is None:
                    t.delta = -0.25
            find_roll_targets(cand, targets, d2)

    def test_sell_puts_random_market_never_raises(self):
        rng = random.Random(1234)
        for _ in range(60):
            syms = [f"Z{i}" for i in range(rng.randint(0, 5))]
            c = FakeBrokerClient(FakeAccount(
                cash=rng.choice([0, -100, 500, 1e6]),
                options_buying_power=rng.choice([0, 1000, 5e5]),
                buying_power=rng.choice([0, 2e6])))
            for u in syms:
                n = rng.randint(0, 4)
                c.option_chain[u] = [
                    make_put(u, rng.choice([10, 50, 200]),
                             dte=rng.choice([7, 21, 45]),
                             bid=rng.choice([0.1, 1.0, 5.0]),
                             ask=rng.choice([0.15, 1.2, 6.0]),
                             delta=rng.choice([-0.05, -0.25, -0.5]),
                             oi=rng.choice([0, 100, 5000]))
                    for _ in range(n)]
                c.stock_trades[u] = rng.choice([10.0, 55.0, 220.0])
            sell_puts(c, syms, rng.choice([0, 5000, 100_000]),
                      execution_config={"limit_enabled": False, "wait_seconds": 0},
                      fund_with_sgov=rng.choice([True, False]))
