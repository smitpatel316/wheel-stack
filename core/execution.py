import logging
import time
from .optionable_sync import push_trade_to_optionable
from .strategy import filter_underlying, filter_options, score_options, select_options
from models.contract import Contract
import numpy as np

import os as _os
if _os.getenv("ENABLE_WHEELER_SYNC", "").lower() in ("1", "true", "yes"):
    try:
        from .wheeler_sync import push_option_to_wheeler
        def _push_wheeler(sym, premium, contracts=1):
            try:
                push_option_to_wheeler(sym, premium, contracts)
            except Exception as e:
                logger.warning("[SWALLOWED] wheeler sync push failed for %s: %r", sym, e)
                pass
    except ImportError as e:
        logging.getLogger(f"strategy.{__name__}").debug("[SWALLOWED] wheeler_sync import unavailable, using no-op push: %r", e)
        def _push_wheeler(sym, premium, contracts=1):
            pass
else:
    # Wheeler tracker was replaced by Optionable; its old REST paths 404.
    # Set ENABLE_WHEELER_SYNC=1 to re-enable.
    def _push_wheeler(sym, premium, contracts=1):
        pass

logger = logging.getLogger(f"strategy.{__name__}")

import math as _math
from .funding_queue import FundingQueue

def _prefund_queue_with_sgov(client, deficit, risk_bp, queue):
    """ONE SGOV market sale per run to pre-fund the next-day CSP queue.

    Replaces same-day funding (removed 2026-08-17): Alpaca options BP counts
    settled cash only (T+1), so a same-day SGOV sale can never fund a CSP in
    the same run — it just churned SGOV (5 sell+buy round-trips in one run).
    Now: queued candidates wait for next-day settled cash, and we sell SGOV
    once, sized to the queue's remaining deficit, so tomorrow's run is funded.

    Never sells when settled-cash BP + already-prefunded cash covers the queue
    (deficit<=0), never more than the risk cap allows, never more than held.
    """
    if deficit <= 0:
        return 0.0
    if queue.broken:
        logger.warning("[SGOV FUND] Queue file unreadable - refusing to pre-fund off unknown state")
        return 0.0
    amount = min(deficit, max(risk_bp, 0))
    if amount <= 0:
        logger.info(f"[SGOV FUND] Risk cap exhausted - cannot pre-fund queue (deficit ${deficit:.0f})")
        return 0.0
    try:
        sgov_qty = 0
        for pos in client.get_positions():
            if getattr(pos, 'symbol', '') == 'SGOV':
                sgov_qty = int(float(pos.qty))
                break
        if sgov_qty <= 0:
            logger.warning("[SGOV FUND] Wanted to pre-fund queue but no SGOV held")
            return 0.0
        try:
            latest = client.get_stock_latest_trade("SGOV")
            trade = latest.get("SGOV") if isinstance(latest, dict) else None
            price = float(getattr(trade, 'price', 0) or (trade.get('p') if isinstance(trade, dict) else 0) or 100.5)
        except Exception as e:
            logger.debug("[SWALLOWED] SGOV latest-trade price fetch failed, using default 100.5: %r", e)
            price = 100.5
        shares = min(sgov_qty, max(1, _math.ceil((amount + 150) / price)))
        logger.info(f"[SGOV FUND] Pre-funding next-day queue: selling {shares} SGOV @ ~${price:.2f} (~${shares*price:.0f}) to cover ${amount:.0f} deficit (settles T+1)")
        order = client.market_sell_qty("SGOV", shares)
        sold = shares * price
        queue.record_prefund(sold)
        logger.info(f"[SGOV FUND] Pre-fund sale submitted (order {getattr(order, 'id', '?')}); queue prefunded total now ${queue.prefunded:.0f}")
        return sold
    except Exception as e:
        logger.warning(f"[SGOV FUND] pre-fund failed: {e}")
        return 0.0


def calc_mid_price(contract_obj) -> float:
    try:
        bid = float(getattr(contract_obj, 'bid_price', 0) or 0)
        ask = float(getattr(contract_obj, 'ask_price', 0) or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        elif bid > 0:
            return bid
        elif ask > 0:
            return ask
        return 0.0
    except Exception as e:
        logger.debug("[SWALLOWED] mid-price calc failed for %s, returning 0.0: %r", getattr(contract_obj, 'symbol', '?'), e)
        return 0.0

def place_limit_or_market_sell(client, contract_obj, strat_logger=None, enable_limit=True, wait_seconds=8):
    """v2.5.1 limit at mid then market fallback"""
    symbol = contract_obj.symbol
    mid = calc_mid_price(contract_obj)
    bid = float(getattr(contract_obj, 'bid_price', 0) or 0)
    
    if not enable_limit or mid <= 0:
        try:
            client.market_sell(symbol)
            return {"type": "market", "price": bid, "mid": mid, "improvement": 0.0}
        except Exception as e:
            logger.warning(f"Market sell failed for {symbol}: {e}")
            raise

    limit_price = round(mid, 2)
    if bid > 0 and limit_price < bid:
        limit_price = bid
    try:
        if hasattr(client, 'limit_sell'):
            order = client.limit_sell(symbol, limit_price)
            logger.info(f"[EXEC] Limit sell {symbol} @ mid ${limit_price:.2f} bid ${bid:.2f} ask ${getattr(contract_obj,'ask_price',0)} mid ${mid:.2f}")
            if wait_seconds > 0 and order:
                time.sleep(wait_seconds)
            # v2.6: verify the fill. The old code returned success on an unfilled
            # resting order — phantom trades in the tracker, and a stale order
            # that could fill later at a bad price or duplicate the next run's sell.
            order_id = getattr(order, 'id', None)
            if order_id:
                try:
                    o = client.get_order(order_id)
                    status = str(getattr(o, 'status', '')).lower()
                    if 'filled' in status:
                        fill_px = float(getattr(o, 'filled_avg_price', 0) or limit_price)
                        improvement = fill_px - bid if bid else 0
                        logger.info(f"[EXEC] Limit FILLED {symbol} @ ${fill_px:.2f} (+${improvement:.2f} vs bid)")
                        return {"type": "limit", "price": fill_px, "mid": mid, "bid": bid, "improvement": improvement}
                    logger.info(f"[EXEC] Limit {symbol} unfilled after {wait_seconds}s (status {status}) - cancel + market fallback")
                    try:
                        client.cancel_order(order_id)
                    except Exception as ce:
                        # Race: filled between the check and the cancel
                        o2 = client.get_order(order_id)
                        if 'filled' in str(getattr(o2, 'status', '')).lower():
                            fill_px = float(getattr(o2, 'filled_avg_price', 0) or limit_price)
                            return {"type": "limit", "price": fill_px, "mid": mid, "bid": bid, "improvement": (fill_px - bid if bid else 0)}
                        logger.debug(f"cancel failed {symbol}: {ce}")
                except Exception as fe:
                    logger.debug(f"fill check failed {symbol}: {fe} - cancel + market to be safe")
                    try:
                        client.cancel_order(order_id)
                    except Exception as e:
                        logger.warning("[SWALLOWED] cancel_order failed for %s (order %s) after fill-check error: %r", symbol, order_id, e)
                        pass
            client.market_sell(symbol)
            return {"type": "market_fallback_unfilled", "price": bid, "mid": mid, "limit_attempt": limit_price, "improvement": 0.0}
        else:
            client.market_sell(symbol)
            return {"type": "market", "price": bid, "mid": mid, "improvement": 0.0}
    except Exception as e:
        logger.warning(f"Limit sell failed for {symbol} mid ${mid}: {e}, market fallback")
        try:
            client.market_sell(symbol)
            return {"type": "market_fallback", "price": bid, "mid": mid, "improvement": 0.0}
        except Exception as e2:
            logger.warning(f"Market fallback also failed {symbol}: {e2}")
            raise

def sell_puts(client, allowed_symbols, buying_power, strat_logger=None, market_context=None, earnings_map=None, dividend_map=None, fundamentals_map=None, vol_map=None, liquidity_map=None, execution_config=None, fund_with_sgov=False, rh_feed=None):
    if not allowed_symbols or buying_power <= 0:
        return
    execution_config = execution_config or {}
    enable_limit = execution_config.get("limit_enabled", True)
    wait_seconds = execution_config.get("wait_seconds", 8)

    logger.info("Searching for put options...")
    filtered_symbols = filter_underlying(client, allowed_symbols, buying_power, earnings_map=earnings_map, dividend_map=dividend_map, fundamentals_map=fundamentals_map, vol_map=vol_map, liquidity_map=liquidity_map, is_call=False)
    if strat_logger:
        strat_logger.set_filtered_symbols(filtered_symbols)
    if len(filtered_symbols) == 0:
        logger.info("No symbols found with sufficient BP or all blocked by earnings/dividend/fundamentals/liquidity filter.")
        return
    option_contracts = client.get_options_contracts(filtered_symbols, 'put')
    snapshots = client.get_option_snapshot([c.symbol for c in option_contracts])

    put_options = []
    for contract in option_contracts:
        snap = snapshots.get(contract.symbol)
        if not snap:
            continue
        try:
            c = Contract.from_contract_snapshot(contract, snap)
            if snap and 'latestQuote' in snap:
                try:
                    c.ask_price = float(snap['latestQuote'].get('ap', 0) or snap['latestQuote'].get('askPrice', 0))
                except Exception as e:
                    logger.debug("[SWALLOWED] ask price parse failed for %s, leaving default: %r", getattr(c, 'symbol', '?'), e)
                    pass
            put_options.append(c)
        except Exception as e:
            logger.debug("[SWALLOWED] put contract build failed for %s, skipping: %r", getattr(contract, 'symbol', '?'), e)
            continue

    put_options = filter_options(put_options, vol_map=vol_map)

    if strat_logger:
        strat_logger.log_put_options([p.to_dict() for p in put_options])
        if market_context:
            strat_logger.set_market_context(market_context)

    if put_options:
        # Track Alpaca's real options buying power separately from the
        # risk-cap BP: SGOV doesn't count as options collateral, so opt_bp
        # is often much lower. Candidates that can't fit are skipped
        # (smaller ones may still fit) instead of wasting doomed orders.
        opt_bp = None
        try:
            acct = client.get_account()
            opt_bp = float(getattr(acct, 'options_buying_power', 0) or 0) or None
            if opt_bp is not None:
                logger.info(f"Alpaca options buying power: ${opt_bp:.0f} (risk-cap BP ${buying_power:.0f})")
        except Exception as e:
            logger.warning(f"Could not read options buying power, using risk-cap BP only: {e}")
        logger.info(f"Scoring {len(put_options)} put options with fund+vol+liq...")
        scores = score_options(put_options, fundamentals_map=fundamentals_map, vol_map=vol_map, liquidity_map=liquidity_map)
        selected = select_options(put_options, scores)

        # Robinhood shadow comparison (read-only, observational). Alpaca stays
        # the source of truth; RH data never influences what we sell.
        if rh_feed is not None:
            for p in selected:
                try:
                    rh_feed.compare_put(p)
                except Exception as e:
                    logger.debug(f"[RH] compare failed for {p.symbol}: {e}")
            logger.info(rh_feed.summary())

        # T+1 funding queue: candidates that settled-cash options BP can't
        # cover THIS run are skipped and queued for next-day funding; one SGOV
        # sale per run pre-funds the queue. The queue is hints only — every
        # entry is re-validated fresh by the scan on later runs.
        queue = FundingQueue().load()
        queue.expire()
        newly_queued = 0
        # Visibility: a queued entry that today's scan doesn't re-select is
        # otherwise silent all run (BAC 2026-08-21 sat pending with no log line).
        if queue.entries:
            logger.info(f"[FUND QUEUE] pending: " + ", ".join(
                f"{e.get('symbol')} ${float(e.get('need', 0)):.0f} (queued {str(e.get('queued_at', '?'))[:10]})"
                for e in queue.entries))

        for p in selected:
            need = 100 * p.strike
            if buying_power < need:
                logger.info(f"Skipping {p.symbol} strike ${p.strike} need ${need} > BP ${buying_power}")
                continue
            if opt_bp is not None and opt_bp < need:
                # Never sell SGOV for a same-day fill (T+1 makes it pointless
                # churn); queue the candidate so tomorrow's settled cash funds it.
                # Cap total queued need at remaining risk headroom: without this
                # the queue over-reserved (3 AAPL contracts, $134k held back
                # against ~$42k headroom on 2026-08-18) and drained the sweep.
                if queue.pending_need() + need > max(buying_power, 0):
                    logger.info(f"[FUND QUEUE] {p.symbol} strike ${p.strike} needs ${need:.0f} - skipped: queue ${queue.pending_need():.0f} + need would exceed remaining risk headroom ${buying_power:.0f}")
                    continue
                score_val = 0
                try:
                    score_val = scores[put_options.index(p)]
                except Exception as e:
                    logger.debug("[SWALLOWED] score lookup failed for %s, using 0: %r", getattr(p, 'symbol', '?'), e)
                    pass
                if queue.add(p.symbol, p.underlying, p.strike, p.expiration, need, score_val):
                    newly_queued += 1
                    logger.info(f"[FUND QUEUE] {p.symbol} strike ${p.strike} needs ${need:.0f} > settled options BP ${opt_bp:.0f} - queued for next-day funding (T+1), re-validated fresh tomorrow")
                else:
                    logger.info(f"[FUND QUEUE] {p.symbol} strike ${p.strike} needs ${need:.0f} > settled options BP ${opt_bp:.0f} - already queued, still unfunded")
                continue
            buying_power -= need
            if opt_bp is not None:
                opt_bp -= need
            score_val = 0
            try:
                idx = put_options.index(p)
                score_val = scores[idx]
            except Exception as e:
                logger.debug("[SWALLOWED] score lookup failed for %s, using 0: %r", getattr(p, 'symbol', '?'), e)
                pass
            mid = calc_mid_price(p)
            logger.info(f"Selling put: {p.symbol} strike ${p.strike} bid ${p.bid_price} mid ${mid:.2f} delta {p.delta} DTE {p.dte} score {score_val:.3f}")

            exec_result = None
            try:
                exec_result = place_limit_or_market_sell(client, p, strat_logger, enable_limit=enable_limit, wait_seconds=wait_seconds)
            except Exception as e:
                logger.warning(f"Sell failed for {p.symbol}: {e}")
                buying_power += need
                if opt_bp is not None:
                    opt_bp += need
                if "buying power" in str(e).lower() or "insufficient" in str(e).lower():
                    # Alpaca's BP disagreed with our local view (stale/None
                    # opt_bp) — queue the candidate for T+1 funding instead of
                    # dropping it silently (F 2026-08-21 vanished this way).
                    if queue.pending_need() + need <= max(buying_power, 0):
                        score_val_q = 0
                        try:
                            score_val_q = scores[put_options.index(p)]
                        except Exception as e2:
                            logger.debug("[SWALLOWED] score lookup failed for %s, using 0: %r", getattr(p, 'symbol', '?'), e2)
                        if queue.add(p.symbol, p.underlying, p.strike, p.expiration, need, score_val_q):
                            newly_queued += 1
                            logger.info(f"[FUND QUEUE] {p.symbol} strike ${p.strike} needs ${need:.0f} - broker BP shortfall, queued for next-day funding (T+1)")
                        else:
                            logger.info(f"[FUND QUEUE] {p.symbol} strike ${p.strike} needs ${need:.0f} - already queued, still unfunded")
                    logger.info(f"Stopping new CSPs: Alpaca reports insufficient buying power after {p.symbol} - remaining candidates skipped this run")
                    break
                continue

            _push_wheeler(p.symbol, (exec_result.get("price") if exec_result else p.bid_price) or 0, contracts=1)
            try:
                push_trade_to_optionable(p.symbol, (exec_result.get("price") if exec_result else p.bid_price) or 0, contracts=1, delta=getattr(p, 'delta', None))
            except Exception as e:
                logger.warning(f"Optionable sync failed for {p.symbol}: {e}")
            queue.mark_filled(p.symbol, underlying=getattr(p, 'underlying', None))

            if strat_logger:
                d = p.to_dict()
                if exec_result:
                    d["execution"] = exec_result
                    d["mid_price"] = exec_result.get("mid")
                    d["limit_price"] = exec_result.get("price")
                    d["price_improvement"] = exec_result.get("improvement", 0)
                strat_logger.log_sold_puts(d)
                strat_logger.log_detailed_trade(d, score=score_val, decision_type="new_put", market_context=market_context)

        # One SGOV sale per run to pre-fund the whole queue (T+1: proceeds
        # settle overnight, tomorrow's run funds queued candidates from
        # settled cash). Never when settled BP + earlier pre-funds cover it.
        if fund_with_sgov and queue.entries:
            deficit = queue.prefund_deficit(opt_bp)
            if deficit > 0:
                _prefund_queue_with_sgov(client, deficit, buying_power, queue)
            else:
                logger.info(f"[FUND QUEUE] {len(queue.entries)} queued (${queue.pending_need():.0f}) already covered by settled BP/prefunded cash - no SGOV sale")
        elif queue.entries:
            logger.info(f"[FUND QUEUE] {len(queue.entries)} queued (${queue.pending_need():.0f}) but SGOV funding disabled - no pre-fund sale")
        if queue.dirty:
            queue.save()
    else:
        logger.info("No put options found with sufficient delta and open interest.")

def sell_calls(client, symbol, purchase_price, stock_qty, strat_logger=None, market_context=None, dividend_map=None, execution_config=None):
    if stock_qty < 100:
        # Log and skip instead of raising: an unhandled raise here killed the
        # whole run (SGOV sweep + Optionable sync never happened).
        logger.warning(f"Skipping covered calls on {symbol}: only {stock_qty} shares held, need 100+ for one contract")
        return
    execution_config = execution_config or {}
    enable_limit = execution_config.get("limit_enabled", True)
    wait_seconds = execution_config.get("wait_seconds", 8)

    logger.info(f"Searching for call options on {symbol}...")
    if dividend_map:
        from .dividend_calendar import is_dividend_risk
        blocked, reason = is_dividend_risk(symbol, dividend_map, block_days=2, dte=30, is_call=True)
        if blocked:
            logger.warning(f"[DIVIDEND] Block call {symbol}: {reason}")
            return

    raw = client.get_options_contracts([symbol], 'call')
    snapshots = client.get_option_snapshot([c.symbol for c in raw])
    contracts = []
    for co in raw:
        snap = snapshots.get(co.symbol)
        if not snap:
            continue
        try:
            c = Contract.from_contract_snapshot(co, snap)
            if snap and 'latestQuote' in snap:
                try:
                    c.ask_price = float(snap['latestQuote'].get('ap', 0) or 0)
                except Exception as e:
                    logger.debug("[SWALLOWED] ask price parse failed for %s, leaving default: %r", getattr(c, 'symbol', '?'), e)
                    pass
            contracts.append(c)
        except Exception as e:
            logger.debug("[SWALLOWED] call contract build failed for %s, skipping: %r", getattr(co, 'symbol', '?'), e)
            continue

    call_options = filter_options(contracts, purchase_price)

    if strat_logger:
        strat_logger.log_call_options([c.to_dict() for c in call_options])

    if call_options:
        scores = score_options(call_options)
        idx = int(np.argmax(scores))
        contract = call_options[idx]
        mid = calc_mid_price(contract)
        logger.info(f"Selling call: {contract.symbol} strike ${contract.strike} bid ${contract.bid_price} mid ${mid:.2f} delta {contract.delta}")

        try:
            exec_result = place_limit_or_market_sell(client, contract, strat_logger, enable_limit=enable_limit, wait_seconds=wait_seconds)
        except Exception as e:
            logger.warning(f"Market sell failed for {contract.symbol}: {e}")
            return

        _push_wheeler(contract.symbol, (exec_result.get("price") if exec_result else contract.bid_price) or 0, contracts=1)
        try:
            push_trade_to_optionable(contract.symbol, (exec_result.get("price") if exec_result else contract.bid_price) or 0, contracts=1, delta=getattr(contract, 'delta', None))
        except Exception as e:
            logger.warning(f"Optionable sync failed for {contract.symbol}: {e}")
        if strat_logger:
            d = contract.to_dict()
            if exec_result:
                d["execution"] = exec_result
            strat_logger.log_sold_calls(d)
            strat_logger.log_detailed_trade(d, score=scores[idx], decision_type="new_call", market_context=market_context)
    else:
        logger.info(f"No viable call options found for {symbol}")

def place_sgov_limit_order(client, side: str, qty: int, logger_obj=None):
    """SGOV sweep orders — MARKET both ways despite the legacy name.
    Per Smit 2026-08-05 and reconfirmed 2026-08-14: SGOV is the sweep vehicle;
    buy with sitting cash, sell when cash is needed (CSP funding or assignment),
    always at market for instant fills. Renaming the function would touch every
    caller for zero behavior change, so the name stays."""
    log = logger_obj or logger
    try:
        if side.lower() == "buy":
            client.market_buy("SGOV", qty)
        else:
            client.market_sell_qty("SGOV", qty)
        log.info(f"[SGOV] Market {side} {qty}")
        return True
    except Exception as e:
        log.warning(f"SGOV {side} {qty} failed: {e}")
        return False
