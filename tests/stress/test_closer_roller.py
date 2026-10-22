"""Stress tests: closer (50% profit taker) and roller."""
from datetime import date, timedelta

import pytest

from core.roller import (RollCandidate, evaluate_roll_need, find_roll_targets,
                         roll_position, _parse_occ, _calc_itm_pct)
from core.closer import evaluate_close_need, close_position
from models.contract import Contract
from tests.stress.fakes import FakeBrokerClient


def _cand(dte=20, entry=1.0, current=0.5, strike=50.0, und=55.0, delta=-0.25,
          is_put=True):
    exp = date.today() + timedelta(days=dte)
    from tests.stress.fakes import make_occ
    itm = _calc_itm_pct(strike, und, is_put)
    profit = (entry - current) / entry if entry else 0
    loss = (current - entry) / entry if entry else 0
    return RollCandidate(
        symbol=make_occ("XYZ", exp, "P" if is_put else "C", strike),
        underlying="XYZ", strike=strike, expiration=exp, dte=dte, qty=-1,
        avg_entry_price=entry, current_price=current, underlying_price=und,
        delta=delta, bid=current, ask=current + 0.05, is_put=is_put,
        itm_pct=itm, loss_pct=loss, profit_pct=profit)


class TestCloserBoundaries:
    def test_cl1_exactly_50pct_closes(self):
        d = evaluate_close_need(_cand(entry=1.0, current=0.50))
        assert d.should_close and d.close_type == "profit_take_50"

    def test_cl2_49_9pct_no_close(self):
        # DTE 30 keeps the time-efficient path (7-21) out of play so only the
        # 50% rule is under test
        d = evaluate_close_need(_cand(entry=1.0, current=0.501, dte=30))
        assert not d.should_close

    def test_cl3_dte3_blocks_normal_close(self):
        d = evaluate_close_need(_cand(entry=1.0, current=0.40, dte=2))
        assert not d.should_close

    def test_cl4_75pct_closes_even_low_dte(self):
        d = evaluate_close_need(_cand(entry=1.0, current=0.20, dte=2))
        assert d.should_close, "75%+ profit must close regardless of DTE"

    def test_cl5_time_efficient_path(self):
        d = evaluate_close_need(_cand(entry=1.0, current=0.55, dte=14))
        assert d.should_close and d.close_type == "profit_take_time"

    def test_cl6_time_path_dte_boundaries(self):
        assert not evaluate_close_need(_cand(entry=1.0, current=0.55, dte=6)).should_close
        assert not evaluate_close_need(_cand(entry=1.0, current=0.55, dte=22)).should_close

    def test_cl6b_time_path_min_abs_profit(self):
        # 45% profit but tiny premium -> abs profit < $0.20 -> no time close
        d = evaluate_close_need(_cand(entry=0.30, current=0.165, dte=14))
        assert not d.should_close

    def test_cl7_zero_current_price_behavior(self):
        # $0 current price => 100% profit => closes. Phantom-$0 protection lives
        # in optionable_sync, but a $0 quote here means "free close" — document it.
        d = evaluate_close_need(_cand(entry=1.0, current=0.0))
        assert d.should_close

    def test_cl10_paper_fees_zero(self):
        d = evaluate_close_need(_cand(entry=1.0, current=0.5))
        assert d.decision_factors["commission_per_contract"] == 0

    def test_cl9_close_failure_returns_false(self):
        c = FakeBrokerClient()

        class Broken(FakeBrokerClient):
            pass

        broken = Broken()
        broken.trade_client.submit_order = lambda req: (_ for _ in ()).throw(Exception("boom"))
        assert close_position(broken, _cand()) is False

    def test_close_submits_buy(self):
        c = FakeBrokerClient()
        cand = _cand()
        assert close_position(c, cand) is True
        assert c.submitted and str(c.submitted[0].side).lower().endswith("buy")


class TestRollerDecisions:
    def test_ro1_otm_under_3pct_rolls(self):
        d = evaluate_roll_need(_cand(strike=50.0, und=51.4))  # 2.7% OTM
        assert d.should_roll and d.roll_type == "defensive" and d.urgency == "medium"

    def test_ro2_otm_exactly_3pct_no_roll(self):
        d = evaluate_roll_need(_cand(strike=50.0, und=51.5, dte=20))  # exactly 3%
        assert not d.should_roll

    def test_ro3_itm_high_urgency(self):
        d = evaluate_roll_need(_cand(strike=50.0, und=49.0, dte=20))
        assert d.should_roll and d.urgency == "high"

    def test_ro4_dte_critical(self):
        d = evaluate_roll_need(_cand(strike=50.0, und=51.4, dte=2))
        assert d.should_roll and d.urgency == "critical"

    def test_ro5_dte1_forces_roll(self):
        d = evaluate_roll_need(_cand(strike=50.0, und=50.2, dte=1))
        assert d.should_roll and d.urgency == "critical"
        assert d.roll_type == "assignment_avoidance"

    def test_ro6_delta_thresholds(self):
        d = evaluate_roll_need(_cand(strike=50.0, und=60.0, delta=-0.55))
        assert d.should_roll
        d2 = evaluate_roll_need(_cand(strike=50.0, und=60.0, delta=-0.65))
        assert d2.urgency == "high"

    def test_ro7_loss_over_100pct_near_money_rolls(self):
        # v2.6: loss>100% only triggers when actually near the money
        d = evaluate_roll_need(_cand(entry=1.0, current=2.5, strike=50.0, und=50.4, delta=-0.45))
        assert d.should_roll and d.roll_type == "defensive"

    def test_ro7b_loss_over_100pct_far_otm_no_roll(self):
        # v2.6: premium doubled but 20% OTM with delta -0.25 -> hold, don't realize early
        d = evaluate_roll_need(_cand(entry=1.0, current=2.5, strike=50.0, und=60.0, delta=-0.25))
        assert not d.should_roll
        # run_strategy greps this exact phrase to emit the [ROLLER] HOLD visibility line
        assert any("premium-loss alone" in r for r in d.reasons)

    def test_ro7c_loss_over_100pct_no_delta_far_otm_no_roll(self):
        d = evaluate_roll_need(_cand(entry=1.0, current=2.5, strike=50.0, und=60.0, delta=None))
        assert not d.should_roll

    def test_ro12_zero_underlying_price_no_crash(self):
        d = evaluate_roll_need(_cand(strike=50.0, und=0.0, dte=20))
        assert d.should_roll  # strike 50, price 0 -> deep ITM -> roll


class TestRollTargets:
    def _target_contract(self, strike=48.0, dte=25, bid=1.0, delta=-0.25):
        return Contract(symbol="XYZ_P_target", underlying="XYZ", strike=strike,
                        dte=dte, bid_price=bid, ask_price=bid + 0.04,
                        delta=delta, oi=500)

    def test_ro8_no_valid_targets_empty(self):
        cand = _cand(dte=20, strike=50, und=49)
        d = evaluate_roll_need(cand)
        assert find_roll_targets(cand, [], d) == []

    def test_ro9_defensive_rejects_higher_strike(self):
        cand = _cand(dte=20, strike=50, und=49)
        d = evaluate_roll_need(cand)
        higher = self._target_contract(strike=52.0, dte=30)
        lower = self._target_contract(strike=48.0, dte=30)
        targets = find_roll_targets(cand, [higher, lower], d)
        assert all(t.strike <= 50.01 for t in targets)
        assert any(t.strike == 48.0 for t in targets)

    def test_ro9b_dte_extension_window(self):
        cand = _cand(dte=20, strike=50, und=49)
        d = evaluate_roll_need(cand)
        too_short = self._target_contract(dte=24)   # < dte + 7
        ok = self._target_contract(dte=30)
        targets = find_roll_targets(cand, [too_short, ok], d)
        assert all(t.dte >= 27 for t in targets)

    def test_ro10_no_debit_even_when_critical(self):
        # Policy change 2026-10-16 (Smit): the v2.5 debit override is gone.
        # Rolls are credit-only in ALL cases - a critical DTE<=1 position
        # with only debit targets gets NO targets and rides to assignment
        # (assignment is the second half of the wheel, never pay to avoid it).
        cand = _cand(dte=1, strike=50, und=50.2, current=1.30)
        d = evaluate_roll_need(cand)
        assert d.urgency == "critical"
        small_debit = self._target_contract(dte=20, bid=1.11)   # net -0.19
        big_debit = self._target_contract(dte=20, bid=1.09)     # net -0.21
        big_debit.symbol = "XYZ_P_target2"
        assert find_roll_targets(cand, [small_debit, big_debit], d) == []
        # ...while a genuine credit target for the same critical position
        # is still found (the override removal must not block credit rolls).
        credit = self._target_contract(dte=20, bid=1.45)        # net +0.15
        credit.symbol = "XYZ_P_target3"
        targets = find_roll_targets(cand, [credit], d)
        assert len(targets) == 1 and abs(targets[0].net_credit - 0.15) < 1e-6

    def test_ro10b_non_critical_never_takes_debit(self):
        cand = _cand(dte=20, strike=50, und=49, current=1.30)
        d = evaluate_roll_need(cand)
        debit = self._target_contract(dte=30, bid=1.20)  # net -0.10
        assert find_roll_targets(cand, [debit], d) == []

    def test_ro13_critical_defensive_rejects_higher_strike(self):
        # B7 regression — live incident 2026-10-22: BAC $59P at DTE 1,
        # 13.6% ITM, loss 380% rolled UP to an $80P because the defensive
        # strike ceiling was skipped at critical urgency (evaluate's loss
        # branch re-labels the DTE<=1 decision back to "defensive", which
        # fell through both strike guards in find_roll_targets). The
        # intrinsic gap counted as a $20.82 "credit". A put must never
        # roll up in strike, at any urgency. If the evaluate() labeling
        # is ever deliberately changed, this test is the tripwire to
        # re-check the guard pairing in find_roll_targets.
        cand = _cand(dte=1, strike=59.0, und=51.0, entry=1.75, current=8.40, delta=None)
        d = evaluate_roll_need(cand)
        assert d.urgency == "critical" and d.roll_type == "defensive"
        higher = self._target_contract(strike=80.0, dte=57, bid=29.22, delta=-0.95)
        assert find_roll_targets(cand, [higher], d) == []

    def test_ro13b_critical_defensive_same_or_lower_strike_still_found(self):
        # The B7 cap must not block legitimate critical rolls: same strike
        # or lower for a genuine credit stays available at DTE 1.
        cand = _cand(dte=1, strike=59.0, und=51.0, entry=1.75, current=8.40, delta=None)
        d = evaluate_roll_need(cand)
        same = self._target_contract(strike=59.0, dte=57, bid=9.00, delta=-0.85)
        lower = self._target_contract(strike=55.0, dte=57, bid=8.75, delta=-0.80)
        lower.symbol = "XYZ_P_target_lower"
        targets = find_roll_targets(cand, [same, lower], d)
        assert {t.strike for t in targets} == {59.0, 55.0}

    def test_ro11_close_before_open_ordering(self):
        c = FakeBrokerClient()
        cand = _cand(dte=1, strike=50, und=50.2, current=1.30)
        d = evaluate_roll_need(cand)
        target = find_roll_targets(cand, [self._target_contract(dte=20, bid=1.45)], d)
        assert target, "critical roll should find the credit target"
        import core.roller as ro
        orig_sleep = ro.time.sleep
        ro.time.sleep = lambda *_: None
        try:
            ok = roll_position(c, cand, target[0])
        finally:
            ro.time.sleep = orig_sleep
        assert ok
        sides = [str(o.side).lower() for o in c.submitted]
        assert sides[0].endswith("buy") and sides[1].endswith("sell"), \
            "close must be submitted before open"

    def _target(self, strike=49.0, dte=56, net_credit=0.11):
        from core.roller import RollTarget
        return RollTarget(
            symbol=f"XYZ_T{int(strike*100)}", strike=strike,
            expiration=date.today() + timedelta(days=dte), dte=dte,
            bid_price=0.87, ask_price=0.90, delta=-0.2, oi=500,
            premium_rate=0.015, annualized_yield=0.1, net_credit=net_credit,
            roll_type="defensive", reasoning="test")

    def test_ro12_preflight_bp_aborts_uncoverable_roll(self):
        # 2026-08-21 (daily review): BAC260904P00061000's close leg filled,
        # then the open leg 403'd on options BP — realized loss taken and the
        # replacement never opened (the funding queue is hints-only; nothing
        # consumes it for a roll leg). Non-critical rolls must abort BEFORE
        # the close when est. post-close BP can't cover the new leg.
        from tests.stress.fakes import FakeAccount
        c = FakeBrokerClient(FakeAccount(options_buying_power=-2000.0))
        # freed = (61 - 0.60)*100 = 6040; post-close BP ~4040 < required 5750
        cand = _cand(dte=15, strike=61.0, und=59.5, current=0.60)
        ok = roll_position(c, cand, self._target(strike=57.5))
        assert not ok
        assert not c.submitted, "close must not happen when the open can't be funded"

    def test_ro12b_preflight_bp_passes_when_funded(self):
        c = FakeBrokerClient()  # default options_buying_power 14000
        cand = _cand(dte=15, strike=50.0, und=49.0, current=1.30)
        import core.roller as ro
        orig_sleep = ro.time.sleep
        ro.time.sleep = lambda *_: None
        try:
            ok = roll_position(c, cand, self._target(strike=49.0))
        finally:
            ro.time.sleep = orig_sleep
        assert ok and len(c.submitted) == 2

    def test_ro12c_critical_dte1_still_closes_anyway(self):
        # Assignment avoidance at DTE<=1 outranks the broken-leg risk: the
        # pre-flight check must NOT block critical rolls.
        from tests.stress.fakes import FakeAccount
        c = FakeBrokerClient(FakeAccount(options_buying_power=-2000.0))
        cand = _cand(dte=1, strike=61.0, und=59.5, current=0.60)
        import core.roller as ro
        orig_sleep = ro.time.sleep
        ro.time.sleep = lambda *_: None
        try:
            ok = roll_position(c, cand, self._target(strike=57.5, dte=20))
        finally:
            ro.time.sleep = orig_sleep
        assert ok and len(c.submitted) == 2


class TestOccParsing:
    def test_parse_occ(self):
        u, exp, pc, strike = _parse_occ("F260821P00014000")
        assert (u, pc, strike) == ("F", "P", 14.0)
        assert exp == date(2026, 8, 21)

    def test_parse_occ_bad(self):
        with pytest.raises(ValueError):
            _parse_occ("NOTANOPTION")


class TestNonePriceGuards:
    """Regression: stress fuzz found two latent crashes on missing quotes."""

    def test_closer_none_current_price_low_dte_no_crash(self):
        cand = _cand(dte=2, entry=1.0, current=0.5)
        cand.current_price = None  # missing quote near expiry
        d = evaluate_close_need(cand)
        assert not d.should_close  # blocked by DTE rule, profit_dollars guarded

    def test_roller_none_current_price_returns_empty(self):
        cand = _cand(dte=1, strike=50, und=49)
        cand.current_price = None
        d = evaluate_roll_need(cand)
        targets = find_roll_targets(cand, [Contract(
            symbol="T", underlying="XYZ", strike=48.0, dte=30,
            bid_price=1.0, ask_price=1.04, delta=-0.25)], d)
        assert targets == []
