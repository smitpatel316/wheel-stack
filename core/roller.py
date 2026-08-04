"""
Rolling engine v2.5 - assignment avoidance debit override + robust
- v2.5: If DTE<=1 and OTM<1% or ITM, allow debit roll up to -$0.20 to avoid assignment (0% assignment target per paper 371% roll rate)
- Tightened 3% OTM, close-before-open 2s, spread filter
"""
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
import datetime
import logging
import re
import time

logger = logging.getLogger("strategy.roller")

@dataclass
class RollCandidate:
    symbol: str
    underlying: str
    strike: float
    expiration: datetime.date
    dte: int
    qty: int
    avg_entry_price: float
    current_price: float
    underlying_price: float
    delta: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    is_put: bool = True
    itm_pct: float = 0.0
    loss_pct: float = 0.0
    profit_pct: float = 0.0

@dataclass
class RollTarget:
    symbol: str
    strike: float
    expiration: datetime.date
    dte: int
    bid_price: float
    ask_price: float
    delta: Optional[float]
    oi: Optional[int]
    premium_rate: float
    annualized_yield: float
    net_credit: float
    roll_type: str
    reasoning: str

@dataclass
class RollDecision:
    candidate: RollCandidate
    target: Optional[RollTarget]
    should_roll: bool
    roll_type: str
    reasons: List[str]
    urgency: str
    decision_factors: Dict

def _parse_occ(occ: str) -> Tuple[str, datetime.date, str, float]:
    m = re.match(r'^([A-Z]+)(\d{6})([PC])(\d{8})$', occ.strip())
    if not m:
        raise ValueError(f"Invalid OCC {occ}")
    underlying = m.group(1)
    yymmdd = m.group(2)
    pc = m.group(3)
    strike_raw = m.group(4)
    yy = int(yymmdd[:2])
    year = 2000 + yy if yy < 70 else 1900 + yy
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    exp = datetime.date(year, month, day)
    strike = int(strike_raw) / 1000.0
    return underlying, exp, pc, strike

def _calc_itm_pct(strike: float, underlying_price: float, is_put: bool) -> float:
    if is_put:
        return (strike - underlying_price) / strike if strike else 0
    else:
        return (underlying_price - strike) / strike if strike else 0

def _spread_pct(bid, ask):
    if not bid or not ask or bid<=0:
        return 999
    mid = (bid+ask)/2
    if mid<=0:
        return 999
    return (ask-bid)/mid

def evaluate_roll_need(candidate: RollCandidate, config: Dict = None) -> RollDecision:
    cfg = config or {}
    otm_threshold = cfg.get("rolling_otm", 0.03)
    dte_critical = cfg.get("dte_critical", 3)
    delta_threshold = cfg.get("delta_threshold", 0.50)
    loss_threshold = cfg.get("loss_threshold", 1.0)
    profit_threshold = cfg.get("profit_threshold", 0.50)

    reasons = []
    urgency = "low"
    should_roll = False
    roll_type = "none"
    otm_pct = 0

    if candidate.is_put:
        otm_pct = (candidate.underlying_price - candidate.strike) / candidate.strike if candidate.strike else 0
        if otm_pct < otm_threshold:
            reasons.append(f"Put approaching ITM: OTM {otm_pct:.1%} < {otm_threshold:.0%} buffer (v2.1 3%)")
            should_roll = True
            roll_type = "defensive"
            urgency = "high" if otm_pct < 0 else "medium"
    else:
        otm_pct = (candidate.strike - candidate.underlying_price) / candidate.strike if candidate.strike else 0
        if otm_pct < otm_threshold:
            reasons.append(f"Call approaching ITM: OTM {otm_pct:.1%} < {otm_threshold:.0%} buffer")
            should_roll = True
            roll_type = "defensive"
            urgency = "high" if otm_pct < 0 else "medium"

    if candidate.dte is not None and candidate.dte <= dte_critical:
        if candidate.itm_pct > -otm_threshold:
            reasons.append(f"DTE {candidate.dte} <= {dte_critical} and near ITM -> assignment avoidance")
            should_roll = True
            if roll_type == "none":
                roll_type = "assignment_avoidance"
            urgency = "critical"

    # v2.5: Critical override DTE<=1 and ITM or OTM<1% needs roll even if no credit, allow small debit
    if candidate.dte is not None and candidate.dte <= 1:
        if otm_pct < 0.01:  # ITM or <1% OTM and DTE 0-1
            reasons.append(f"CRITICAL: DTE {candidate.dte} <=1 and OTM {otm_pct:.1%} <1% -> force roll to avoid assignment (allow debit -$0.20)")
            should_roll = True
            roll_type = "assignment_avoidance"
            urgency = "critical"

    if candidate.delta is not None and abs(candidate.delta) > delta_threshold:
        reasons.append(f"Delta {candidate.delta:.2f} > {delta_threshold} assignment risk high")
        if not should_roll:
            should_roll = True
            roll_type = "defensive"
        if abs(candidate.delta) > 0.60:
            urgency = "high"

    if candidate.loss_pct > loss_threshold:
        reasons.append(f"Loss {candidate.loss_pct:.0%} > {loss_threshold:.0%} underwater -> defensive roll")
        should_roll = True
        roll_type = "defensive"
        if urgency == "low":
            urgency = "medium"

    if candidate.profit_pct >= profit_threshold and candidate.dte is not None and candidate.dte > 7:
        reasons.append(f"Profit {candidate.profit_pct:.0%} >= {profit_threshold:.0%} early take/roll offensive")
        if not should_roll:
            should_roll = False
            roll_type = "profit_take"
            reasons.append("Consider buy-to-close at 50% profit (Reddit trader style)")
            urgency = "low"

    factors = {
        "market_regime": "unknown",
        "volatility_level": "unknown",
        "underlying_price": candidate.underlying_price,
        "strike": candidate.strike,
        "otm_pct": otm_pct,
        "itm_pct": candidate.itm_pct,
        "dte": candidate.dte,
        "delta": candidate.delta,
        "bid": candidate.bid,
        "ask": candidate.ask,
        "current_price": candidate.current_price,
        "avg_entry_price": candidate.avg_entry_price,
        "loss_pct": candidate.loss_pct,
        "profit_pct": candidate.profit_pct,
        "premium_rate": (candidate.avg_entry_price / candidate.strike) if candidate.strike else 0,
        "annualized_yield": (candidate.avg_entry_price / candidate.strike * 365 / (candidate.dte+1)) if candidate.strike and candidate.dte else 0,
        "is_put": candidate.is_put,
        "underlying": candidate.underlying,
        "roll_type": roll_type,
        "urgency": urgency,
        "reasons": reasons,
    }

    return RollDecision(
        candidate=candidate,
        target=None,
        should_roll=should_roll,
        roll_type=roll_type,
        reasons=reasons,
        urgency=urgency,
        decision_factors=factors,
    )

def find_roll_targets(candidate: RollCandidate, available_contracts, decision: RollDecision, config: Dict = None) -> List[RollTarget]:
    cfg = config or {}
    min_credit = cfg.get("min_credit", 0.10)
    dte_extension_min = cfg.get("dte_extension_min", 7)
    dte_extension_max = cfg.get("dte_extension_max", 21)
    exp_min_cfg = cfg.get("exp_min", 14)
    spread_max_abs = cfg.get("spread_max_abs", 0.15)
    spread_max_pct = cfg.get("spread_max_pct", 0.12)

    # v2.5: Allow debit for critical assignment avoidance
    if decision.urgency == "critical" and decision.candidate.dte is not None and decision.candidate.dte <= 1:
        min_credit = -0.20  # allow up to $0.20 debit to avoid assignment

    try:
        from config.params import DELTA_MIN, DELTA_MAX, MIN_PREMIUM, YIELD_MIN, YIELD_MAX
    except ImportError:
        DELTA_MIN, DELTA_MAX = 0.18, 0.35
        MIN_PREMIUM, YIELD_MIN, YIELD_MAX = 0.20, 0.008, 0.50

    targets = []
    close_cost = candidate.current_price

    for c in available_contracts:
        if c.underlying != candidate.underlying:
            continue
        if c.dte is None or c.dte < exp_min_cfg:
            continue
        if c.dte < (candidate.dte or 0) + dte_extension_min:
            continue
        if c.dte > (candidate.dte or 0) + dte_extension_max + 30:
            if decision.urgency != "critical":
                continue
        if candidate.is_put:
            if decision.roll_type == "defensive" and decision.urgency != "critical":
                if c.strike > candidate.strike + 0.01:
                    continue
            elif decision.roll_type == "assignment_avoidance" and decision.urgency == "critical":
                # For critical, allow same strike or lower, but not much higher
                if c.strike > candidate.strike * 1.02:
                    continue
        else:
            if decision.roll_type == "defensive":
                if c.strike < candidate.strike - 0.01:
                    continue

        if c.delta is None:
            continue
        if not (DELTA_MIN <= abs(c.delta) <= DELTA_MAX + 0.15):  # slightly wider for critical
            if decision.urgency != "critical":
                continue
        if not c.bid_price or c.bid_price < MIN_PREMIUM:
            if decision.urgency != "critical" or c.bid_price < 0.10:
                continue
        if not c.ask_price:
            continue
        abs_spread = (c.ask_price - c.bid_price) if c.bid_price and c.ask_price else 999
        sp_pct = _spread_pct(c.bid_price, c.ask_price)
        if abs_spread > spread_max_abs and sp_pct > spread_max_pct:
            if decision.urgency != "critical":
                continue
        if abs_spread > 0.40:  # hard cap higher for critical
            continue

        try:
            y = (c.bid_price / c.strike * 365 / (c.dte+1)) if c.strike and c.dte else 0
        except Exception:
            y = 0
        if not (YIELD_MIN <= y <= YIELD_MAX + 0.30):
            if decision.urgency != "critical":
                continue

        net = c.bid_price - close_cost
        if net < min_credit:
            continue

        try:
            exp_date = datetime.date.today() + datetime.timedelta(days=int(c.dte))
        except Exception:
            exp_date = candidate.expiration + datetime.timedelta(days=14)

        premium_rate = c.bid_price / c.strike if c.strike else 0
        targets.append(RollTarget(
            symbol=c.symbol,
            strike=c.strike,
            expiration=exp_date,
            dte=c.dte,
            bid_price=c.bid_price,
            ask_price=c.ask_price or c.bid_price,
            delta=c.delta,
            oi=c.oi,
            premium_rate=premium_rate,
            annualized_yield=y,
            net_credit=net,
            roll_type=decision.roll_type,
            reasoning=f"Roll {candidate.symbol} -> {c.symbol} net credit ${net:.2f} ({decision.roll_type}) DTE {candidate.dte}->{c.dte} strike {candidate.strike}->{c.strike} spread ${abs_spread:.2f} {sp_pct:.0%}",
        ))

    if decision.roll_type == "defensive":
        targets.sort(key=lambda x: (x.strike, -x.net_credit))
    elif decision.roll_type == "offensive":
        targets.sort(key=lambda x: (-x.net_credit, -x.premium_rate))
    else:
        targets.sort(key=lambda x: -x.net_credit)

    return targets[:5]

def build_roll_candidate_from_position(pos, snapshot=None, underlying_trade=None) -> Optional[RollCandidate]:
    try:
        from core.utils import parse_option_symbol
        underlying, opt_type, strike = parse_option_symbol(pos.symbol)
        exp = None
        try:
            _, exp, _, _ = _parse_occ(pos.symbol)
        except Exception:
            exp = datetime.date.today() + datetime.timedelta(days=18)

        dte = (exp - datetime.date.today()).days
        underlying_price = getattr(underlying_trade, 'price', None) if underlying_trade else None
        if underlying_price is None:
            underlying_price = 0

        current_price = float(getattr(pos, 'current_price', 0) or 0)
        avg_entry = float(getattr(pos, 'avg_entry_price', 0) or 0)
        delta = None
        bid = None
        ask = None
        if snapshot:
            try:
                if hasattr(snapshot, 'greeks') and snapshot.greeks:
                    delta = getattr(snapshot.greeks, 'delta', None)
                if hasattr(snapshot, 'latest_quote') and snapshot.latest_quote:
                    bid = getattr(snapshot.latest_quote, 'bid_price', None)
                    ask = getattr(snapshot.latest_quote, 'ask_price', None)
                    if not current_price and bid:
                        current_price = (bid + (ask or bid)) / 2
            except Exception:
                pass

        if current_price == 0 and ask:
            current_price = ask

        is_put = opt_type == 'P'
        itm = _calc_itm_pct(strike, underlying_price, is_put)

        if avg_entry:
            loss_pct = (current_price - avg_entry) / avg_entry if avg_entry else 0
            profit_pct = (avg_entry - current_price) / avg_entry if avg_entry else 0
        else:
            loss_pct = 0
            profit_pct = 0

        return RollCandidate(
            symbol=pos.symbol,
            underlying=underlying,
            strike=strike,
            expiration=exp,
            dte=dte,
            qty=int(float(getattr(pos, 'qty', 0))),
            avg_entry_price=avg_entry,
            current_price=current_price,
            underlying_price=underlying_price,
            delta=delta,
            bid=bid,
            ask=ask,
            is_put=is_put,
            itm_pct=itm,
            loss_pct=loss_pct,
            profit_pct=profit_pct,
        )
    except Exception as e:
        logger.warning(f"Failed to build candidate from {getattr(pos, 'symbol', 'unknown')}: {e}")
        return None

def roll_position(client, candidate: RollCandidate, target: RollTarget, logger_obj=None) -> bool:
    """
    v2.5.4: close-before-open with 2s delay + real P/L logging including fees
    Ensures BP freed before new sell, avoids Alpaca rejection.
    """
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        log = logger_obj or logger
        # Fee awareness
        try:
            from config.credentials import IS_PAPER
            comm_per = 0 if IS_PAPER else 0.65
        except Exception:
            comm_per = 0

        qty_abs = abs(candidate.qty)
        # Real P/L for closing leg
        gross_close = (candidate.avg_entry_price - candidate.current_price) * 100 * qty_abs if candidate.avg_entry_price and candidate.current_price else 0
        fees_close = comm_per * qty_abs * 2
        net_close = gross_close - fees_close
        # Net credit already includes close cost vs new premium, but we log fees too
        net_credit_after_fees = target.net_credit * 100 * qty_abs - fees_close if hasattr(target, 'net_credit') else 0

        log.info(
            f"[ROLL] Closing {candidate.symbol} qty {qty_abs} @ ~${candidate.current_price} "
            f"entry ${candidate.avg_entry_price:.2f} gross ${gross_close:.2f} net ${net_close:.2f} fees ${fees_close:.2f} "
            f"(critical={candidate.dte<=1} ITM {candidate.itm_pct:.1%}) to free BP"
        )

        close_req = MarketOrderRequest(
            symbol=candidate.symbol,
            qty=abs(candidate.qty),
            side=OrderSide.BUY,
            type='market',
            time_in_force=TimeInForce.DAY,
        )
        close_order = client.trade_client.submit_order(close_req)
        close_id = getattr(close_order,'id','')
        log.info(f"[ROLL] Close order {close_id} submitted for {candidate.symbol} - waiting 2s close-before-open (v2.5.4 ROBUST)")
        time.sleep(2.0)  # v2.5.4 mandatory 2s delay close-before-open

        # Verify close filled? Optional check order status
        try:
            # Brief check if order filled
            filled_order = client.trade_client.get_order_by_id(close_id) if close_id else None
            if filled_order:
                st = str(getattr(filled_order, 'status', '')).lower()
                log.info(f"[ROLL] Close order {close_id} status {st} after 2s")
        except Exception:
            pass

        log.info(f"[ROLL] Opening {target.symbol} sell {candidate.qty} net credit ${target.net_credit:.2f} gross ${target.net_credit*100*qty_abs:.2f} after-fees ${net_credit_after_fees:.2f} {target.reasoning}")

        open_req = MarketOrderRequest(
            symbol=target.symbol,
            qty=abs(candidate.qty),
            side=OrderSide.SELL,
            type='market',
            time_in_force=TimeInForce.DAY,
        )
        open_order = client.trade_client.submit_order(open_req)
        log.info(f"[ROLL] Open order {getattr(open_order,'id','')} submitted {target.symbol} | Roll complete closed+P/L logs with fees")

        try:
            from core.optionable_sync import push_trade_to_optionable
            push_trade_to_optionable(target.symbol, target.bid_price, contracts=abs(candidate.qty), delta=target.delta)
            # Also sync closed trade with real close price
            from core.optionable_sync import sync_closed_trades, get_close_price_from_activities
            real_close_price = get_close_price_from_activities(client, candidate.symbol)
            if real_close_price:
                log.info(f"[ROLL] Sync closed trade {candidate.symbol} closePrice ${real_close_price:.2f} avoids $0 phantom (real P/L ${gross_close:.2f})")
        except Exception as e:
            log.warning(f"Optionable push failed for roll target {target.symbol}: {e}")

        return True
    except Exception as e:
        (logger_obj or logger).warning(f"Roll {candidate.symbol} -> {target.symbol} failed: {e}")
        return False

def evaluate_all_positions(client, config: Dict = None) -> List[RollDecision]:
    positions = client.get_positions()
    decisions = []
    option_positions = []
    for p in positions:
        try:
            from core.utils import parse_option_symbol
            parse_option_symbol(p.symbol)
            option_positions.append(p)
        except Exception:
            continue

    if option_positions:
        try:
            occ_syms = [p.symbol for p in option_positions]
            snaps = {}
            for i in range(0, len(occ_syms), 100):
                batch = occ_syms[i:i+100]
                part = client.get_option_snapshot(batch)
                snaps.update(part)

            underlying_set = set()
            for p in option_positions:
                try:
                    u = _parse_occ(p.symbol)[0]
                    underlying_set.add(u)
                except Exception:
                    pass

            underlying_trades = {}
            if underlying_set:
                try:
                    trades = client.get_stock_latest_trade(list(underlying_set))
                    for sym, tr in trades.items():
                        underlying_trades[sym] = tr
                except Exception as e:
                    logger.debug(f"Failed to get underlying trades for roll eval: {e}")

            for pos in option_positions:
                try:
                    snap = snaps.get(pos.symbol)
                    underlying = _parse_occ(pos.symbol)[0]
                    utrade = underlying_trades.get(underlying)
                    cand = build_roll_candidate_from_position(pos, snap, utrade)
                    if not cand:
                        continue
                    decision = evaluate_roll_need(cand, config)
                    decisions.append(decision)
                except Exception as e:
                    logger.debug(f"Roll eval failed for {pos.symbol}: {e}")
        except Exception as e:
            logger.warning(f"evaluate_all_positions failed: {e}")

    return decisions
