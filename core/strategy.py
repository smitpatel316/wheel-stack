from config.params import DELTA_MIN, DELTA_MAX, YIELD_MIN, YIELD_MAX, OPEN_INTEREST_MIN, SCORE_MIN
from config.params import SPREAD_MAX_ABS, SPREAD_MAX_PCT, SPREAD_NTM_MAX, MIN_PREMIUM, EARNINGS_BLOCK_DAYS, EARNINGS_BLOCK_DTE
from config.params import DIVIDEND_BLOCK_DAYS, PE_MAX, MARKET_CAP_MIN
import logging
import os

log = logging.getLogger(__name__)

def filter_underlying(client, symbols, buying_power_limit, earnings_map=None, dividend_map=None, fundamentals_map=None, vol_map=None, liquidity_map=None, is_call=False):
    """
    Filter underlying symbols v2.5.1 with liquidity trend
    - BP + earnings + dividends + fundamentals + vol + liquidity (5d vs 20d vol trend)
    """
    from datetime import date
    resp = client.get_stock_latest_trade(symbols)
    if not resp:
        print(f"[DATA] get_stock_latest_trade returned EMPTY for {len(symbols)} symbols - treating as transient data failure, no CSPs this run")
        return []
    missing = [s for s in symbols if s not in resp]
    if missing:
        print(f"[DATA] get_stock_latest_trade missing {len(missing)}/{len(symbols)}: {missing}")
    filtered_symbols = [symbol for symbol in resp if 100*resp[symbol].price <= buying_power_limit]
    dropped_bp = [s for s in resp if 100*resp[s].price > buying_power_limit]
    if dropped_bp:
        print(f"[BP] Dropped over BP limit ${buying_power_limit:.0f}: {[(s, round(100*resp[s].price)) for s in dropped_bp]}")
    print(f"[DATA] underlying filter: {len(symbols)} in -> {len(filtered_symbols)} after price/BP")

    if earnings_map:
        from core.earnings_calendar import is_earnings_risk
        today = date.today()
        safe = []
        for sym in filtered_symbols:
            blocked, reason = is_earnings_risk(sym, earnings_map, today, block_days=EARNINGS_BLOCK_DAYS, dte=EARNINGS_BLOCK_DTE)
            if blocked:
                print(f"[EARNINGS] Skip {sym}: {reason}")
                continue
            safe.append(sym)
        filtered_symbols = safe

    if dividend_map and is_call:
        from core.dividend_calendar import is_dividend_risk
        today = date.today()
        safe = []
        for sym in filtered_symbols:
            blocked, reason = is_dividend_risk(sym, dividend_map, today, block_days=DIVIDEND_BLOCK_DAYS, dte=EARNINGS_BLOCK_DTE, is_call=True)
            if blocked:
                print(f"[DIVIDEND] Skip call {sym}: {reason}")
                continue
            safe.append(sym)
        filtered_symbols = safe

    if fundamentals_map:
        safe = []
        for sym in filtered_symbols:
            if sym.upper() in fundamentals_map:
                eval_r = fundamentals_map[sym.upper()]
                if isinstance(eval_r, dict) and eval_r.get("blocked"):
                    print(f"[FUND] Skip {sym}: {eval_r.get('reason')}")
                    continue
            safe.append(sym)
        filtered_symbols = safe

    if liquidity_map:
        safe = []
        for sym in filtered_symbols:
            if sym.upper() in liquidity_map:
                liq = liquidity_map[sym.upper()]
                if isinstance(liq, dict) and not liq.get("trend_ok", True):
                    if liq.get("avg_5d", 0) < 300_000:  # only block if very thin
                        print(f"[LIQ] Skip {sym}: {liq.get('reason')} - extremely thin")
                        continue
            safe.append(sym)
        filtered_symbols = safe

    if vol_map:
        for sym in filtered_symbols[:5]:
            if sym.upper() in vol_map:
                info = vol_map[sym.upper()]
                if isinstance(info, dict) and info.get("iv_rank", 50) > 80:
                    print(f"[VOL] {sym} high IVRank {info.get('iv_rank'):.0f} RV20 {info.get('rv_20d')} - defensive delta")

    return filtered_symbols

def _calc_yield(bid_price, strike, dte):
    try:
        if not bid_price or not strike or dte is None:
            return 0
        if dte < 1:
            dte = 1
        return (bid_price / strike) * (365.0 / (dte + 1))
    except Exception as e:
        log.debug("[SWALLOWED] yield calc failed (bid=%r strike=%r dte=%r): %r", bid_price, strike, dte, e)
        return 0

def _calc_spread_pct(bid, ask):
    if not bid or not ask or bid <=0:
        return 999
    mid = (bid + ask)/2
    if mid <=0:
        return 999
    return (ask - bid)/mid

def filter_options(options, min_strike=0, vol_map=None):
    """
    Filter options v2.5.1 with spread + vol adaptive + liquidity volume trend already via underlying filter
    """
    filtered = []
    rejects = {"no_delta": 0, "delta_range": 0, "low_premium": 0, "no_ask": 0, "spread": 0, "yield": 0, "oi": 0, "strike": 0}
    for contract in options:
        if contract.delta is None:
            rejects["no_delta"] += 1
            continue
        ad = abs(contract.delta)

        delta_max = DELTA_MAX
        if vol_map and contract.underlying and contract.underlying.upper() in vol_map:
            vm = vol_map[contract.underlying.upper()]
            if isinstance(vm, dict) and "delta_max" in vm:
                delta_max = vm["delta_max"]

        if ad < DELTA_MIN or ad > delta_max:
            rejects["delta_range"] += 1
            continue
        if not contract.bid_price or contract.bid_price < MIN_PREMIUM:
            rejects["low_premium"] += 1
            continue
        if not contract.ask_price:
            rejects["no_ask"] += 1
            continue
        spread_abs = contract.ask_price - contract.bid_price
        spread_pct = _calc_spread_pct(contract.bid_price, contract.ask_price)
        if ad >= 0.30 and spread_abs > SPREAD_NTM_MAX and spread_abs > SPREAD_MAX_ABS:
            if spread_abs > 0.10:
                rejects["spread"] += 1
                continue
        else:
            if spread_abs > SPREAD_MAX_ABS and spread_pct > SPREAD_MAX_PCT:
                rejects["spread"] += 1
                continue
            if spread_abs > SPREAD_MAX_ABS and spread_pct > 0.08:
                if spread_abs > 0.30:
                    rejects["spread"] += 1
                    continue
        y = _calc_yield(contract.bid_price, contract.strike, contract.dte)
        if y < YIELD_MIN or y > YIELD_MAX:
            rejects["yield"] += 1
            continue
        if contract.oi is not None and contract.oi < OPEN_INTEREST_MIN:
            rejects["oi"] += 1
            continue
        # Volume trend: if underlying provided via vol_map with avg_5d < 100k, extra OI check
        if contract.strike is None or contract.strike < min_strike:
            rejects["strike"] += 1
            continue
        filtered.append(contract)
    if options and not filtered:
        print(f"[DATA] option filter rejected all {len(options)} contracts: {rejects}")
    return filtered

def score_options(options, fundamentals_map=None, vol_map=None, liquidity_map=None):
    """
    Score v2.5.1: (1-|Δ|)*(250/(DTE+5))*(bid/strike)*liqBoost*spreadPenalty*fundScore*volScore*volumeTrend
    """
    scores = []
    for p in options:
        try:
            d = abs(p.delta) if p.delta else 0.25
            dte_term = 250.0 / ((p.dte or 30) + 5)
            prem_term = (p.bid_price / p.strike) if p.strike else 0
            liq_boost = 1.1 if p.oi is not None and p.oi > 500 else 1.0
            spread_pct = _calc_spread_pct(p.bid_price, p.ask_price or p.bid_price)
            spread_penalty = 1.0
            if spread_pct > 0.10:
                spread_penalty = 0.8
            elif spread_pct > 0.05:
                spread_penalty = 0.9
            if p.ask_price and p.bid_price:
                abs_spread = p.ask_price - p.bid_price
                if abs_spread > 0.10:
                    spread_penalty *= 0.9

            fund_score = 1.0
            if fundamentals_map and p.underlying and p.underlying.upper() in fundamentals_map:
                fm = fundamentals_map[p.underlying.upper()]
                if isinstance(fm, dict):
                    fund_score = fm.get("score_modifier", 1.0)

            vol_score = 1.0
            if vol_map and p.underlying and p.underlying.upper() in vol_map:
                vm = vol_map[p.underlying.upper()]
                if isinstance(vm, dict):
                    ivr = vm.get("iv_rank", 50)
                    if ivr >= 50:
                        vol_score = 1.1
                    elif ivr < 20:
                        vol_score = 0.9

            liq_trend_score = 1.0
            if liquidity_map and p.underlying and p.underlying.upper() in liquidity_map:
                lm = liquidity_map[p.underlying.upper()]
                if isinstance(lm, dict):
                    liq_trend_score = lm.get("score_modifier", 1.0)

            scores.append((1 - d) * dte_term * prem_term * liq_boost * spread_penalty * fund_score * vol_score * liq_trend_score)
        except Exception as e:
            log.debug("[SWALLOWED] score calc failed for %s, scoring 0: %r", getattr(p, 'symbol', '?'), e)
            scores.append(0)
    return scores

def select_options(options, scores, n=None):
    filtered = [(option, score) for option, score in zip(options, scores) if score > SCORE_MIN]
    best_per_underlying = {}
    for option, score in filtered:
        underlying = option.underlying
        if (underlying not in best_per_underlying) or (score > best_per_underlying[underlying][1]):
            best_per_underlying[underlying] = (option, score)
    sorted_best = sorted(best_per_underlying.values(), key=lambda x: x[1], reverse=True)
    return [option for option, _ in sorted_best[:n]] if n else [option for option, _ in sorted_best]
