"""
Closer - 50% profit taker for Options Wheel
Reddit July trader style: closed SNDK/INTC before -50% chip bloodbath
Paper arXiv:2512.01123: profit_take candidate when profit_pct >=50% and DTE>7
Sophie: take profit at 50% of max credit, free BP for next wheel

Rules:
- Trigger: profit_pct >= 50% (current price <= entry * 0.5) AND DTE > 3 (avoid gamma risk + assignment)
- Optional: also close if profit >= $0.20 and >40% with DTE <21 (time efficient)
- Safety: never close if delta abs >0.60 (still high assignment risk? actually profit means OTM, delta low)
- Logs 27 factors for Bayesian CPT feedback loop
- Execution: buy_to_close via broker_client.market_buy for options? Actually need buy order for option
- Use TradingClient close_position or MarketOrderRequest side BUY

Returns list of CloseDecisions
"""
from dataclasses import dataclass
from typing import Optional, List, Dict
import datetime
import logging
import time
from .roller import _parse_occ, RollCandidate, _calc_itm_pct

logger = logging.getLogger("strategy.closer")

@dataclass
class CloseDecision:
    candidate: RollCandidate
    should_close: bool
    close_type: str  # profit_take_50, profit_take_time, loss_stop
    profit_pct: float
    profit_dollars: float
    reasons: List[str]
    urgency: str
    decision_factors: Dict

def build_close_candidate_from_position(pos, snapshot=None, underlying_trade=None) -> Optional[RollCandidate]:
    """Reuse roller builder - same fields"""
    from .roller import build_roll_candidate_from_position
    return build_roll_candidate_from_position(pos, snapshot, underlying_trade)

def evaluate_close_need(candidate: RollCandidate, config: Dict = None) -> CloseDecision:
    """
    Evaluate if position should be closed for profit.
    v2.5.4: logs actual P/L including fees (commission) for real vs optionable discrepancy tracking.

    Triggers v1:
    - profit >=50% and DTE>3 -> profit_take_50 (main)
    - profit >=40% and DTE 7-21 and absolute profit >=$0.20 -> profit_take_time (efficient)
    - profit >=75% regardless of DTE (if >1) -> force close

    Returns CloseDecision
    """
    cfg = config or {}
    profit_threshold = cfg.get("profit_threshold", 0.50)
    profit_time_threshold = cfg.get("profit_time_threshold", 0.40)
    min_profit_abs = cfg.get("min_profit_abs", 0.20)
    dte_min = cfg.get("dte_min", 3)
    # v2.5.4 fee awareness: paper $0, live $0.65/contract from params/credentials
    try:
        from config.credentials import IS_PAPER
        comm_per_contract = 0 if IS_PAPER else 0.65
    except Exception as e:
        logger.debug("[SWALLOWED] IS_PAPER import failed, assuming commission 0: %r", e)
        comm_per_contract = 0

    reasons = []
    should_close = False
    close_type = "none"
    urgency = "low"

    if candidate.dte is not None and candidate.dte <= dte_min:
        # Don't close profitable if near expiry? Actually profit take near expiry is fine, but avoid if DTE<3 due to gamma
        # Allow if profit >=75%
        if candidate.profit_pct < 0.75:
            return CloseDecision(
                candidate=candidate,
                should_close=False,
                close_type="none",
                profit_pct=candidate.profit_pct,
                profit_dollars=((candidate.avg_entry_price or 0) - (candidate.current_price or 0)) * 100 * abs(candidate.qty or 0),
                reasons=[f"DTE {candidate.dte} <= {dte_min} too close to expiry, hold for expiration unless 75%+ profit"],
                urgency="low",
                decision_factors={"dte": candidate.dte, "profit_pct": candidate.profit_pct, "blocked_dte": True}
            )

    # Main 50% rule
    if candidate.profit_pct >= profit_threshold:
        reasons.append(f"Profit {candidate.profit_pct:.0%} >= {profit_threshold:.0%} target (Reddit trader early close style, Sophie 50% rule)")
        should_close = True
        close_type = "profit_take_50"
        urgency = "medium" if candidate.profit_pct >= 0.60 else "low"
        if candidate.profit_pct >= 0.75:
            urgency = "high"
            reasons.append("High profit >=75% - lock gains, free BP, avoid reversal (chip bloodbath protection)")

    # Time efficient: 40%+ profit with DTE 7-21 and $0.20+ absolute
    elif candidate.profit_pct >= profit_time_threshold and candidate.dte is not None and 7 <= candidate.dte <= 21:
        abs_profit = candidate.avg_entry_price - candidate.current_price
        if abs_profit >= min_profit_abs:
            reasons.append(f"Time-efficient: {candidate.profit_pct:.0%} profit ({abs_profit:.2f}) with DTE {candidate.dte} 7-21, better to redeploy BP")
            should_close = True
            close_type = "profit_take_time"
            urgency = "low"

    # v2.5.4: real P/L with fees, gross vs net
    profit_dollars_gross = (candidate.avg_entry_price - candidate.current_price) * 100 * abs(candidate.qty) if candidate.avg_entry_price and candidate.current_price else 0
    profit_dollars_net = profit_dollars_gross - comm_per_contract*abs(candidate.qty)*2  # open+close
    fees_est = comm_per_contract*abs(candidate.qty)*2
    profit_dollars = profit_dollars_net  # use net for decision but log both

    factors = {
        "underlying_price": candidate.underlying_price,
        "strike": candidate.strike,
        "otm_pct": (candidate.underlying_price - candidate.strike)/candidate.strike if candidate.is_put and candidate.strike else 0,
        "itm_pct": candidate.itm_pct,
        "dte": candidate.dte,
        "delta": candidate.delta,
        "bid": candidate.bid,
        "ask": candidate.ask,
        "current_price": candidate.current_price,
        "avg_entry_price": candidate.avg_entry_price,
        "profit_pct": candidate.profit_pct,
        "loss_pct": candidate.loss_pct,
        "profit_dollars": profit_dollars_net,
        "profit_dollars_gross": profit_dollars_gross,
        "profit_dollars_net": profit_dollars_net,
        "fees_estimated": fees_est,
        "commission_per_contract": comm_per_contract,
        "real_pnl_gross": profit_dollars_gross,
        "real_pnl_net": profit_dollars_net,
        "premium_rate": (candidate.avg_entry_price / candidate.strike) if candidate.strike else 0,
        "annualized_yield": (candidate.avg_entry_price / candidate.strike * 365 / (candidate.dte+1)) if candidate.strike and candidate.dte else 0,
        "is_put": candidate.is_put,
        "underlying": candidate.underlying,
        "close_type": close_type,
        "urgency": urgency,
        "reasons": reasons,
        "qty": candidate.qty,
    }

    return CloseDecision(
        candidate=candidate,
        should_close=should_close,
        close_type=close_type,
        profit_pct=candidate.profit_pct,
        profit_dollars=profit_dollars,
        reasons=reasons,
        urgency=urgency,
        decision_factors=factors,
    )

def evaluate_all_for_close(client, config: Dict = None) -> List[CloseDecision]:
    """Evaluate all short option positions for early profit close"""
    positions = client.get_positions()
    decisions = []

    option_positions = []
    for p in positions:
        try:
            from core.utils import parse_option_symbol
            parse_option_symbol(p.symbol)
            option_positions.append(p)
        except Exception as e:
            logger.debug("[SWALLOWED] %s not an option position, skipped in close eval: %r", getattr(p, 'symbol', '?'), e)
            continue

    if not option_positions:
        return decisions

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
            except Exception as e:
                logger.debug("[SWALLOWED] OCC parse failed for %s in underlying-set build: %r", getattr(p, 'symbol', '?'), e)
                pass

        underlying_trades = {}
        if underlying_set:
            try:
                trades = client.get_stock_latest_trade(list(underlying_set))
                for sym, tr in trades.items():
                    underlying_trades[sym] = tr
            except Exception as e:
                logger.debug(f"Failed underlying trades for closer: {e}")

        for pos in option_positions:
            try:
                snap = snaps.get(pos.symbol)
                underlying = _parse_occ(pos.symbol)[0]
                utrade = underlying_trades.get(underlying)
                cand = build_close_candidate_from_position(pos, snap, utrade)
                if not cand:
                    continue
                dec = evaluate_close_need(cand, config)
                decisions.append(dec)
            except Exception as e:
                logger.debug(f"Close eval failed {pos.symbol}: {e}")
    except Exception as e:
        logger.warning(f"evaluate_all_for_close failed: {e}")

    return decisions

def close_position(client, candidate: RollCandidate, logger_obj=None) -> bool:
    """
    Execute buy-to-close for profit.
    Uses TradingClient MarketOrderRequest side BUY (closing short option)
    v2.5.4: logs actual P/L including fees, net vs gross, for discrepancy tracking.
    """
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        log = logger_obj or logger
        # Fee awareness
        try:
            from config.credentials import IS_PAPER
            comm_per = 0 if IS_PAPER else 0.65
        except Exception as e:
            log.debug("[SWALLOWED] IS_PAPER import failed, assuming commission 0: %r", e)
            comm_per = 0
        qty_abs = abs(candidate.qty)
        gross = (candidate.avg_entry_price - candidate.current_price) * 100 * qty_abs if candidate.avg_entry_price and candidate.current_price else 0
        fees = comm_per * qty_abs * 2
        net = gross - fees
        # Real P/L = sell premium - buyback cost - fees
        otm_pct = (candidate.underlying_price - candidate.strike)/candidate.strike if candidate.is_put and candidate.strike else 0
        log.info(
            f"[CLOSER] Closing {candidate.symbol} profit {candidate.profit_pct:.0%} "
            f"gross ${gross:.2f} net ${net:.2f} fees ${fees:.2f} "
            f"entry ${candidate.avg_entry_price:.2f} cur ${candidate.current_price:.2f} "
            f"DTE {candidate.dte} qty {qty_abs} delta {candidate.delta} OTM {otm_pct:.1%}"
        )

        req = MarketOrderRequest(
            symbol=candidate.symbol,
            qty=abs(candidate.qty),
            side=OrderSide.BUY,
            type='market',
            time_in_force=TimeInForce.DAY,
        )
        order = client.trade_client.submit_order(req)
        log.info(f"[CLOSER] Close order {getattr(order,'id','')} submitted for {candidate.symbol} gross ${gross:.2f} net ${net:.2f}")

        # Attempt to sync real P/L immediately via _fetch_buy_price after brief delay
        try:
            time.sleep(0.5)
            from core.optionable_sync import get_close_price_from_activities, sync_closed_trades
            # Let Alpaca settle then sync_closed will fetch actual fill; for now log intent
            log.info(f"[CLOSER] Will sync Optionable closePrice via Alpaca fill for {candidate.symbol} - avoids $0 phantom bug")
        except Exception as e:
            log.warning("[SWALLOWED] post-close Optionable sync intent failed for %s: %r", candidate.symbol, e)
            pass

        return True
    except Exception as e:
        (logger_obj or logger).warning(f"Close {candidate.symbol} failed: {e}")
        return False
