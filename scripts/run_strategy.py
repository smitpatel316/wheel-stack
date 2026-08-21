from pathlib import Path
from core.broker_client import BrokerClient
from core.execution import sell_puts, sell_calls, place_sgov_limit_order
from core.state_manager import update_state, calculate_risk, calculate_exposures, TREASURY_SYMBOLS, load_roll_counts, save_roll_counts, prune_roll_counts, MAX_ROLLS_PER_LINEAGE
from config.credentials import ALPACA_API_KEY, ALPACA_SECRET_KEY, IS_PAPER
from config.params import MAX_RISK, EARNINGS_BLOCK_DAYS, EARNINGS_BLOCK_DTE, EARNINGS_CACHE_DAYS, EARNINGS_ENABLED, DIVIDEND_ENABLED, DIVIDEND_BLOCK_DAYS, FUNDAMENTALS_ENABLED, IV_RANK_ENABLED, LIMIT_ORDER_ENABLED, LIMIT_WAIT_SECONDS, SGOV_ENABLED
from app_logging.strategy_logger import StrategyLogger
from app_logging.logger_setup import setup_logger
from core.optionable_sync import sync_alpaca_equity_to_optionable, sync_sgov_to_optionable, alive as optionable_alive, sync_closed_trades
from core.cli_args import parse_args
from core.activities_sync import sync_dividends_and_interest, sync_option_events
from core.context_analyzer import analyze_context, adapt_params, save_context_log
from core.roller import evaluate_all_positions, find_roll_targets, roll_position
from core.closer import evaluate_all_for_close, close_position
from core.strategy import filter_options
from core.earnings_calendar import build_cache as earnings_build_cache, get_earnings_risk_report
from models.contract import Contract
import math
import os
import json

TOTAL_CAPITAL = 100_000

def sync_sgov_real(client, logger, risk_override=None):
    """v2.5.3 SGOV as SPAXX/Robinhood sweep - interest on sitting collateral
    Fidelity model: All cash including put collateral sits in SPAXX earning ~5%, still counts as CSP collateral.
    Robinhood model: Fixed interest on cash balance.
    SGOV is wrapper for that interest. We sweep 99% of cash into SGOV, log daily/monthly yield.
    """
    try:
        positions = client.get_positions()
        acct = client.get_account()
        if risk_override is not None:
            put_exp, long_stock, risk = 0, 0, risk_override
            try:
                pe, ls, r = calculate_exposures(positions)
                put_exp, long_stock = pe, ls
                if risk_override == 0:
                    risk = r
            except Exception as e:
                logger.warning("[SWALLOWED] calculate_exposures failed in SGOV sweep, zeroing put/long exposure (risk_override=%s): %r", risk_override, e)
                put_exp, long_stock = 0, 0
        else:
            put_exp, long_stock, risk = calculate_exposures(positions)

        sgov_qty = 0
        sgov_price = 100.72
        sgov_mv = 0
        for p in positions:
            if getattr(p, 'symbol', '') == 'SGOV':
                try:
                    sgov_qty = int(float(getattr(p, 'qty', 0)))
                    sgov_price = float(getattr(p, 'current_price', sgov_price) or sgov_price)
                    sgov_mv = sgov_qty * sgov_price
                except Exception as e:
                    logger.debug("[SWALLOWED] SGOV position field parse failed, keeping qty/price defaults: %r", e)
                    pass
        try:
            latest = client.get_stock_latest_trade("SGOV")
            trade = latest.get("SGOV") if isinstance(latest, dict) else None
            if trade:
                pr = getattr(trade, 'price', None) or (trade.get('price') if isinstance(trade, dict) else None)
                if pr:
                    sgov_price = float(pr)
                    sgov_mv = sgov_qty * sgov_price
        except Exception as e:
            logger.info(f"SGOV price fetch fallback: {e}")

        cash = float(acct.cash)
        equity = float(acct.equity)
        stock_bp = float(getattr(acct, 'buying_power', 0) or 0)
        opt_bp_sweep = float(getattr(acct, 'options_buying_power', 0) or 0)
        cashBuffer = 500.0  # keep $500 cash for fees

        # T+1 funding queue: cash earmarked for queued CSP candidates must NOT
        # be swept back into SGOV (that buy-back was the churn removed
        # 2026-08-17). Reserve the part not already covered by settled BP.
        queue_reserve = 0.0
        prefund_pending_qty = 0
        try:
            from core.funding_queue import FundingQueue
            _q = FundingQueue().load()
            _q.expire()
            _q.save()
            queue_reserve = _q.reserve_amount(opt_bp_sweep)
            # A pre-fund market sale that already FILLED is invisible to the
            # open-orders guard below, and Alpaca's position endpoint lags the
            # fill — without this the sweep double-sells in the same run
            # (2026-08-21 morning: pre-fund sold 10, sweep sold 10 more 29s
            # later off the stale position read).
            prefund_pending_qty = _q.pending_prefund_qty()
            if queue_reserve > 0:
                logger.info(f"[SGOV] Holding back ${queue_reserve:.0f} from sweep for {len(_q.entries)} queued CSP candidate(s) (T+1 funding)")
        except Exception as _qe:
            logger.debug(f"[SGOV] funding-queue reserve check failed: {_qe}")

        # v2.5.3 sweep model: all cash including put collateral earns interest via SGOV/SPAXX
        # Fidelity SPAXX sweep: cash + money market both count as CSP collateral, so we sweep 99% cash to SGOV
        # Alpaca paper limitation: SGOV is stock, not cash, so stock BP limits sweep (see failed buy log above)
        total_liquid = cash + sgov_mv  # total money market + cash
        target_sweep_mv_ideal = max(0, total_liquid - cashBuffer - queue_reserve)  # ideal Fidelity model: sweep all (minus queued-CSP reserve)
        # Alpaca realistic: limited by stock BP because SGOV is equity, not cash collateral
        # BP constrains NEW purchases only — holding existing SGOV consumes no
        # buying power. The old cap (max(0, stock_bp-1000) + sgov_mv) forced a
        # sale of (1000 - stock_bp) dollars whenever stock BP dipped under $1k
        # (2026-08-21: forced sells of 10+6 shares in the morning/midday runs,
        # then a 1-share buy-back in the afternoon — pure churn).
        buy_capacity = max(0.0, stock_bp - 1000)  # keep $1k buffer for stock BP
        target_sweep_mv_real = min(target_sweep_mv_ideal, sgov_mv + buy_capacity)
        # Use realistic for actual order
        target_sweep_mv = target_sweep_mv_real
        target_shares = math.floor(target_sweep_mv / sgov_price) if target_sweep_mv >= sgov_price else 0

        # Old idle model for reference: idle = TOTAL_CAPITAL - risk
        idle_old = TOTAL_CAPITAL - risk
        target_old = max(0, idle_old)
        target_old_shares = math.floor(target_old / sgov_price) if target_old >= sgov_price else 0

        diff = target_shares - sgov_qty
        # SGOV yield ~5.22% APY, monthly div ~0.43%, daily accrual
        sgov_yield_apy = 0.0522
        daily_interest_ideal = target_sweep_mv_ideal * sgov_yield_apy / 365.0
        monthly_interest_ideal = target_sweep_mv_ideal * sgov_yield_apy / 12.0
        annual_interest_ideal = target_sweep_mv_ideal * sgov_yield_apy
        daily_interest_real = target_sweep_mv * sgov_yield_apy / 365.0
        monthly_interest_real = target_sweep_mv * sgov_yield_apy / 12.0

        logger.info(f"[SGOV SWEEP] Fidelity SPAXX/RH wrapper: cash ${cash:.0f} + SGOV {sgov_qty}x${sgov_price:.2f}=${sgov_mv:.0f} total liquid ${total_liquid:.0f} stockBP ${stock_bp:.0f} buffer ${cashBuffer:.0f}")
        logger.info(f"[SGOV] target {target_shares} shares ${target_sweep_mv:.0f} diff {diff} ideal would be {math.floor(target_sweep_mv_ideal/sgov_price)} ${target_sweep_mv_ideal:.0f} (old idle model {target_old_shares} ${target_old:.0f}) | put ${put_exp:.0f} long ${long_stock:.0f} idle_old ${idle_old:.0f} risk ${risk:.0f}")
        logger.info(f"[SGOV YIELD] Ideal Fidelity sweep APY {sgov_yield_apy*100:.2f}% on ${target_sweep_mv_ideal:.0f} = ${daily_interest_ideal:.2f}/day ${monthly_interest_ideal:.2f}/mo ${annual_interest_ideal:.0f}/yr | Real Alpaca limited ${target_sweep_mv:.0f} = ${daily_interest_real:.2f}/day ${monthly_interest_real:.2f}/mo")
        if abs(target_sweep_mv_ideal - target_sweep_mv) > 1000:
            logger.info(f"[SGOV] Alpaca paper limitation: SGOV is stock not cash collateral, stockBP ${stock_bp:.0f} limits sweep vs Fidelity SPAXX where MMF counts as collateral - ideal ${target_sweep_mv_ideal:.0f} real ${target_sweep_mv:.0f} diff ${target_sweep_mv_ideal-target_sweep_mv:.0f}")

        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            open_orders = client.trade_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=50))
            sgov_open_buy = sum(int(float(o.qty)) for o in open_orders if getattr(o,'symbol','')=='SGOV' and str(getattr(o,'side','')).lower().find('buy')>=0)
            sgov_open_sell = sum(int(float(o.qty)) for o in open_orders if getattr(o,'symbol','')=='SGOV' and str(getattr(o,'side','')).lower().find('sell')>=0)
            if prefund_pending_qty > 0:
                sgov_open_sell += prefund_pending_qty
            if sgov_open_buy > 0:
                logger.info(f"[SGOV] Existing open BUY SGOV {sgov_open_buy} - skip duplicate")
                diff = 0
            elif sgov_open_sell > 0:
                # Pending SGOV sells (e.g. the funding-queue pre-fund sale) are
                # already committed against this position. Without accounting
                # for them the sweep double-sells and Alpaca rejects with
                # "insufficient qty available" (2026-08-18 midday run: tried
                # to sell 541 with only 196 available after a 416-share
                # pre-fund). Never BUY while sells are pending either — the
                # position is mid-flight down, buying here would be churn.
                effective_qty = max(0, sgov_qty - sgov_open_sell)
                new_diff = min(0, target_shares - effective_qty)
                new_diff = -min(abs(new_diff), effective_qty)
                if new_diff != diff:
                    logger.info(f"[SGOV] {sgov_open_sell} shares already pending sale - adjusted sweep diff {diff} -> {new_diff}")
                    diff = new_diff
        except Exception as e:
            logger.debug(f"Open order check failed: {e}")

        if diff > 0:
            logger.info(f"[SGOV SWEEP] Buying {diff} SGOV @ ${sgov_price:.2f} to earn interest on ${diff*sgov_price:.0f} collateral (Fidelity SPAXX sweep)")
            place_sgov_limit_order(client, "buy", diff, logger_obj=logger)
        elif diff < 0:
            logger.info(f"[SGOV] Selling {abs(diff)} SGOV at market (target {target_shares} < held {sgov_qty}: rebalance to sweep target, frees ${abs(diff)*sgov_price:.0f} cash)")
            place_sgov_limit_order(client, "sell", abs(diff), logger_obj=logger)
        else:
            logger.info(f"[SGOV] At sweep target {target_shares} shares earning ${monthly_interest_real:.2f}/mo - perfect SPAXX wrapper")
    except Exception as e:
        logger.warning(f"SGOV sync failed: {e}")

def main():
    args = parse_args()
    strat_logger = StrategyLogger(enabled=args.strat_log)
    logger = setup_logger(level=args.log_level, to_file=args.log_to_file)
    strat_logger.set_fresh_start(args.fresh_start)

    # Engine dashboard push: collects this run's account snapshot + per-symbol
    # scan funnel from the log stream and POSTs them to Optionable at end of
    # run (core/optionable_dashboard_sync.py). Never raises; display-only.
    from core.optionable_dashboard_sync import EngineDashboardPush
    dash_push = EngineDashboardPush()
    dash_push.install()

    SYMBOLS_FILE = Path(__file__).parent.parent / "config" / "symbol_list.txt"
    with open(SYMBOLS_FILE, 'r') as file:
        SYMBOLS = [line.strip() for line in file.readlines()]

    client = BrokerClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY, paper=IS_PAPER)

    # Market clock check v2.5
    is_market_open = True
    try:
        from alpaca.trading.client import TradingClient
        tc = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=IS_PAPER)
        clock = tc.get_clock()
        is_market_open = getattr(clock, 'is_open', True)
        logger.info(f"[CLOCK] Market is_open={is_market_open} next_close={getattr(clock,'next_close',None)} next_open={getattr(clock,'next_open',None)}")
        if hasattr(strat_logger, 'log_entry'):
            strat_logger.log_entry["market_open"] = is_market_open
    except Exception as e:
        logger.debug(f"Clock check failed, assume open: {e}")

    # --- Phase 0.0 Robinhood shadow feed (read-only validation, 2026-08-17) ---
    # Compares RH quotes against Alpaca for the watchlist + every CSP
    # candidate. Pure observation: RH data never influences trade decisions.
    # Any failure disables the feed for the run; Alpaca remains the truth.
    rh_feed = None
    try:
        from core.robinhood_feed import RobinhoodFeed
        _rf = RobinhoodFeed.from_env(log=logger)
        if _rf.enabled:
            rh_feed = _rf
            logger.info("[RH] shadow feed enabled - comparing Alpaca vs Robinhood quotes this run")
            try:
                _trades = client.get_stock_latest_trade(SYMBOLS)
                _px = {}
                for s in SYMBOLS:
                    t = _trades.get(s) if isinstance(_trades, dict) else None
                    p = getattr(t, 'price', None) or (t.get('price') if isinstance(t, dict) else None)
                    if p:
                        _px[s] = float(p)
                if _px:
                    rh_feed.compare_underlyings(_px)
                    logger.info(f"[RH] underlying compare done for {len(_px)} symbols")
            except Exception as _ue:
                logger.warning(f"[RH] underlying comparison failed (continuing): {_ue}")
        else:
            logger.info(f"[RH] shadow feed disabled: {_rf.disabled_reason}")
    except Exception as e:
        logger.warning(f"[RH] feed init failed (continuing without RH): {e}")

    # --- Phase 0.1 Earnings v2.5 503 proof + NVDA ---
    earnings_map = {}
    earnings_report = {}
    try:
        if EARNINGS_ENABLED:
            logger.info(f"[EARNINGS] Fetching earnings calendar {len(SYMBOLS)} symbols next {EARNINGS_CACHE_DAYS}d (Finnhub primary + 503 retain + Alpha fallback)")
            earnings_report = get_earnings_risk_report(SYMBOLS, block_days=EARNINGS_BLOCK_DAYS, days_ahead=EARNINGS_CACHE_DAYS, dte_default=EARNINGS_BLOCK_DTE)
            from datetime import datetime
            for sym, info in earnings_report.items():
                if info.get("earnings_date"):
                    try:
                        from datetime import date
                        earnings_map[sym] = datetime.fromisoformat(info["earnings_date"]).date()
                    except Exception as e:
                        logger.debug("[SWALLOWED] earnings date parse failed for %s (%r): %r", sym, info.get("earnings_date"), e)
                        pass
            blocked = [(s,i["earnings_date"],i["reason"]) for s,i in earnings_report.items() if i["blocked"]]
            if blocked:
                logger.info(f"[EARNINGS] Blocked {len(blocked)}: {[(b[0], b[1]) for b in blocked]}")
                for sym, edate, reason in blocked[:10]:
                    logger.info(f"  - {sym} {edate}: {reason}")
                # Critical alert for TODAY/TOMORROW
                today_blocked = [b for b in blocked if "TODAY" in b[2] or "TOMORROW" in b[2]]
                if today_blocked:
                    logger.warning(f"🚨 [EARNINGS] CRITICAL TODAY/TOMORROW earnings: {today_blocked} - alert Telegram")
            else:
                logger.info(f"[EARNINGS] No blocked, found {len(earnings_map)} dates")
            if hasattr(strat_logger, 'log_entry'):
                strat_logger.log_entry["earnings_report"] = earnings_report
        else:
            logger.info("[EARNINGS] Disabled")
    except Exception as e:
        logger.warning(f"[EARNINGS] Failed: {e}")

    # --- Phase 0.2 Dividend v2.5 OVERVIEW ex-div ---
    dividend_map = {}
    dividend_report = {}
    try:
        if DIVIDEND_ENABLED:
            logger.info(f"[DIVIDEND] Fetching dividend calendar via Alpha OVERVIEW ExDiv + DIVIDENDS")
            from core.dividend_calendar import get_dividend_risk_report, build_cache as div_build
            dividend_map = div_build(SYMBOLS, days_ahead=30)
            dividend_report = get_dividend_risk_report(SYMBOLS, block_days=DIVIDEND_BLOCK_DAYS, days_ahead=30, is_call=False)
            div_blocked = [s for s,i in dividend_report.items() if i.get("blocked")]
            if div_blocked:
                logger.info(f"[DIVIDEND] Puts ex-div log: {div_blocked}")
            from core.dividend_calendar import get_dividend_risk_report as div_call_report
            call_report = div_call_report(SYMBOLS, block_days=DIVIDEND_BLOCK_DAYS, days_ahead=30, is_call=True)
            call_blocked = [s for s,i in call_report.items() if i.get("blocked")]
            if call_blocked:
                logger.info(f"[DIVIDEND] Calls blocked ex-div: {call_blocked}")
            else:
                logger.info(f"[DIVIDEND] No calls blocked, dividends found {len(dividend_map)} {list(dividend_map.items())[:3]}")
            if hasattr(strat_logger, 'log_entry'):
                strat_logger.log_entry["dividend_report"] = dividend_report
                strat_logger.log_entry["dividend_map"] = {k:v.isoformat() for k,v in dividend_map.items()}
        else:
            logger.info("[DIVIDEND] Disabled")
    except Exception as e:
        logger.warning(f"[DIVIDEND] Failed: {e}")

    # --- Phase 0.3 Fundamentals v2.5 P/E + Debt/Eq via BALANCE_SHEET ---
    fundamentals_map = {}
    fundamentals_report = {}
    try:
        if FUNDAMENTALS_ENABLED:
            logger.info(f"[FUND] Fetching fundamentals P/E + D/E via Alpha OVERVIEW + BALANCE_SHEET")
            from core.fundamentals import get_fundamentals_report
            fundamentals_report = get_fundamentals_report(SYMBOLS)
            fundamentals_map = fundamentals_report
            blocked_f = [s for s,r in fundamentals_report.items() if r.get("blocked")]
            if blocked_f:
                logger.info(f"[FUND] Blocked extreme: {blocked_f}")
            # Log Debt/Eq high
            high_de = []
            for s,r in fundamentals_report.items():
                try:
                    de = r.get("data",{}).get("DebtEquity")
                    if de and float(de) > 0.7:
                        high_de.append(f"{s} D/E {float(de):.2f}")
                except Exception as e:
                    logger.debug("[SWALLOWED] fundamentals DebtEquity parse failed for %s: %r", s, e)
                    pass
            if high_de:
                logger.info(f"[FUND] High leverage D/E>0.7: {high_de[:5]}")
            if hasattr(strat_logger, 'log_entry'):
                strat_logger.log_entry["fundamentals_report"] = {k: {"blocked":v.get("blocked"), "reason":v.get("reason"), "score_mod":v.get("score_modifier"), "de":v.get("data",{}).get("DebtEquity")} for k,v in fundamentals_report.items()}
        else:
            logger.info("[FUND] Disabled")
    except Exception as e:
        logger.warning(f"[FUND] Failed: {e}")

    # --- Phase 0.4 Volatility IV Rank proxy ---
    vol_map = {}
    vol_report = {}
    try:
        if IV_RANK_ENABLED:
            logger.info(f"[VOL] Fetching IV Rank proxy via Alpha TIME_SERIES_DAILY")
            from core.volatility import get_volatility_report
            vol_report = get_volatility_report(SYMBOLS, vix=15.6)
            vol_map = vol_report
            high_iv = [s for s,r in vol_report.items() if r.get("iv_rank",50)>=50]
            low_iv = [s for s,r in vol_report.items() if r.get("iv_rank",50)<20]
            if high_iv:
                logger.info(f"[VOL] High IV >=50 favorable: {high_iv[:8]}")
            if low_iv:
                logger.info(f"[VOL] Low IV <20 wait: {low_iv[:8]}")
            if hasattr(strat_logger, 'log_entry'):
                strat_logger.log_entry["volatility_report"] = vol_report
        else:
            logger.info("[VOL] Disabled")
    except Exception as e:
        logger.warning(f"[VOL] Failed: {e}")

    # --- Phase 0.5 Liquidity Volume Trend 5d vs 20d v2.5.2 ---
    liquidity_map = {}
    try:
        logger.info("[LIQ] Checking volume trend 5d vs 20d via Alpha TIME_SERIES")
        from core.liquidity import get_liquidity_report
        liquidity_map = get_liquidity_report(SYMBOLS[:10])
        drying = [s for s,r in liquidity_map.items() if not r.get("trend_ok", True)]
        if drying:
            logger.info(f"[LIQ] Drying detected: {drying}")
            for s in drying[:5]:
                logger.info(f"  - {s}: {liquidity_map[s].get('reason')} 5d {liquidity_map[s].get('avg_5d',0)/1e6:.1f}M 20d {liquidity_map[s].get('avg_20d',0)/1e6:.1f}M")
        else:
            logger.info(f"[LIQ] All volume trends OK top10 {list(liquidity_map.keys())[:5]}")
        if hasattr(strat_logger, 'log_entry'):
            strat_logger.log_entry["liquidity_report"] = {k: {"trend_ok":v.get("trend_ok"), "score":v.get("score_modifier"), "reason":v.get("reason"), "avg5":v.get("avg_5d"), "avg20":v.get("avg_20d")} for k,v in liquidity_map.items()}
    except Exception as e:
        logger.warning(f"[LIQ] Failed: {e}")

    # --- Phase 0.6 Critical Earnings Alert Check v2.5.2 ---
    try:
        crit_file = Path(__file__).parent.parent / "logs" / "earnings_critical_alert.json"
        if crit_file.exists():
            crit_data = json.loads(crit_file.read_text())
            crit_list = crit_data.get("critical", [])
            if crit_list:
                logger.warning(f"🚨 [EARNINGS] CRITICAL ALERT from webhook: {crit_data.get('alert')} - Telegram alert required!")
                # Keep file for 24h then auto-delete? Leave for next run to consume, but log
                if hasattr(strat_logger, 'log_entry'):
                    strat_logger.log_entry["critical_earnings_alert"] = crit_data
                # Delete after reading if older than 1 day? We keep but log
                # For perfect robust, send explicit alert line that Hermes cron delivery will forward to Telegram
                print(f"🚨🚨🚨 TELEGRAM ALERT: Earnings TODAY/TOMORROW {crit_list} - wheel blocked")
    except Exception as e:
        logger.debug(f"Critical alert check failed: {e}")

    # --- Phase 1 Context Analyzer v2.2 Yahoo v8 VIX ---
    market_ctx = None
    adapted = {}
    try:
        logger.info("[CONTEXT] Analyzing market regime Yahoo v8 VIX real")
        market_ctx = analyze_context(client=client, symbols=SYMBOLS, use_llm=False)
        adapted = adapt_params(market_ctx)
        strat_logger.set_market_context(market_ctx)
        if market_ctx and earnings_map:
            market_ctx.decision_factors["earnings_count"] = len(earnings_map)
            market_ctx.decision_factors["earnings_blocked"] = [s for s,i in earnings_report.items() if i.get("blocked")]
        if market_ctx and dividend_map:
            market_ctx.decision_factors["dividend_count"] = len(dividend_map)
            market_ctx.decision_factors["dividend_ex"] = list(dividend_map.keys())[:5]
        if market_ctx and fundamentals_map:
            market_ctx.decision_factors["fundamentals_blocked"] = [s for s,r in fundamentals_map.items() if r.get("blocked")]
        if market_ctx and vol_report:
            from core.volatility import adapt_delta_by_iv
            real_vix = market_ctx.vix
            for sym in SYMBOLS:
                if sym.upper() in vol_map:
                    vol_map[sym.upper()] = adapt_delta_by_iv(sym, vol_map, vix=real_vix)
            market_ctx.decision_factors["iv_rank_high"] = [s for s,r in vol_map.items() if r.get("iv_rank",0)>=50]
            market_ctx.decision_factors["debt_equity_high"] = [s for s,r in fundamentals_map.items() if (r.get("data",{}).get("DebtEquity") or 0)>0.7][:5] if fundamentals_map else []
        save_context_log(market_ctx)
        logger.info(f"[CONTEXT] Regime={market_ctx.market_regime} VIX={market_ctx.vix:.1f} {market_ctx.vix_level} Vol={market_ctx.volatility_level} Tech={market_ctx.technical_position} source={market_ctx.decision_factors.get('vix_source')} SPY5d={market_ctx.decision_factors.get('spy_5d'):.2%}")
        logger.info(f"[CONTEXT] Adapted: size {adapted.get('POSITION_SIZE_PCT','15')}% delta max {adapted.get('DELTA_MAX')} risk {adapted.get('MAX_RISK')} rolling {adapted.get('ROLLING_OTM',0.03)*100:.0f}%")
    except Exception as e:
        logger.warning(f"Context analyzer failed, using defaults: {e}")

    effective_max_risk = adapted.get("MAX_RISK", MAX_RISK)

    if args.fresh_start:
        logger.info("Running in fresh start mode — liquidating all positions.")
        client.liquidate_all_positions()
        allowed_symbols = SYMBOLS
        buying_power = effective_max_risk
        if earnings_map:
            from core.earnings_calendar import is_earnings_risk
            from datetime import date
            today = date.today()
            allowed_symbols = [s for s in allowed_symbols if not is_earnings_risk(s, earnings_map, today, EARNINGS_BLOCK_DAYS, EARNINGS_BLOCK_DTE)[0]]
            logger.info(f"[EARNINGS] Fresh start allowed after filter: {len(allowed_symbols)}/{len(SYMBOLS)}")
    else:
        positions = client.get_positions()
        strat_logger.add_current_positions(positions)
        current_risk = calculate_risk(positions)
        states = update_state(positions)
        strat_logger.add_state_dict(states)
        # v2.5: Use options_buying_power for more accurate BP
        acct = client.get_account()
        opt_bp = float(getattr(acct, 'options_buying_power', 0) or 0)
        stock_bp = float(getattr(acct, 'buying_power', 0) or 0)
        strat_logger.set_buying_power(effective_max_risk - current_risk)
        logger.info(f"[ACCOUNT] Equity ${float(acct.equity):.2f} Cash ${float(acct.cash):.0f} Stock BP ${stock_bp:.0f} Options BP ${opt_bp:.0f} Risk ${current_risk:.0f}/{effective_max_risk}")

        # Record equity snapshot for the Optionable income/benchmark dashboard
        try:
            import json as _json
            from datetime import datetime as _dt
            eq_path = Path(__file__).resolve().parent.parent / "logs" / "equity_history.json"
            hist = []
            if eq_path.exists():
                try:
                    hist = _json.loads(eq_path.read_text())
                except Exception as e:
                    logger.warning("[SWALLOWED] equity history load failed, restarting history from empty: %r", e)
                    hist = []
            hist.append({"t": _dt.now().astimezone().isoformat(), "equity": float(acct.equity)})
            eq_path.write_text(_json.dumps(hist[-5000:]))

            # SGOV holding snapshot (accrual-accurate income tracking for the dashboard)
            if SGOV_ENABLED:
                try:
                    sgov_qty, sgov_avg = 0.0, None
                    for _p in client.get_positions():
                        if getattr(_p, "symbol", None) == "SGOV":
                            sgov_qty = float(getattr(_p, "qty", 0))
                            try:
                                sgov_avg = float(getattr(_p, "avg_entry_price"))
                            except Exception as e:
                                logger.debug("[SWALLOWED] SGOV avg_entry_price parse failed, using None: %r", e)
                                sgov_avg = None
                    sg_path = Path(__file__).resolve().parent.parent / "logs" / "sgov_history.json"
                    sh = []
                    if sg_path.exists():
                        try:
                            sh = _json.loads(sg_path.read_text())
                        except Exception as e:
                            logger.warning("[SWALLOWED] SGOV history load failed, restarting history from empty: %r", e)
                            sh = []
                    sh.append({"t": _dt.now().astimezone().isoformat(), "shares": sgov_qty, "avg": sgov_avg})
                    sg_path.write_text(_json.dumps(sh[-5000:]))
                except Exception as _e2:
                    logger.warning(f"[ACCOUNT] sgov snapshot failed: {_e2}")
        except Exception as _e:
            logger.warning(f"[ACCOUNT] equity snapshot failed: {_e}")

        # --- Activities check for assignments (OPASN) v2.5 ---
        try:
            from alpaca.trading.requests import GetAccountActivitiesRequest
            from alpaca.trading.enums import ActivityType
            # Use MCP style but via client if available
            activities = []
            try:
                # Try via trade_client if method exists
                pass
            except Exception as e:
                logger.debug("[SWALLOWED] trade_client activities probe failed: %r", e)
                pass
            # Log any long stock that indicates assignment
            long_non_treasury = [s for s, st in states.items() if st["type"]=="long_shares" and s not in TREASURY_SYMBOLS]
            if long_non_treasury:
                logger.info(f"[ASSIGNMENT] Detected {len(long_non_treasury)} long stock positions (possible assignment): {long_non_treasury} -> will sell covered calls next")
        except Exception as e:
            logger.debug(f"Assignment check failed: {e}")

        # --- Phase 2 Closer 50% ---
        try:
            logger.info("[CLOSER] Evaluating 50% profit take Option A conservative")
            close_decisions = evaluate_all_for_close(client, config={
                "profit_threshold": 0.50,
                "profit_time_threshold": 0.40,
                "min_profit_abs": 0.20,
                "dte_min": 3,
            })
            should_close = [d for d in close_decisions if d.should_close]
            if hasattr(strat_logger, 'log_entry'):
                strat_logger.log_entry["close_decisions"] = [
                    {"symbol": d.candidate.symbol, "should_close": d.should_close, "type": d.close_type, "profit_pct": d.profit_pct, "profit_$": d.profit_dollars, "reasons": d.reasons, "urgency": d.urgency, "dte": d.candidate.dte}
                    for d in close_decisions
                ]
            if should_close:
                logger.info(f"[CLOSER] {len(should_close)} ready for profit take:")
                for d in sorted(should_close, key=lambda x: -x.profit_pct)[:5]:
                    logger.info(f"  - {d.candidate.symbol} {d.close_type} profit {d.profit_pct:.0%} (${d.profit_dollars:.0f}) DTE {d.candidate.dte} urgency {d.urgency}: {d.reasons}")
                if not is_market_open:
                    logger.info(f"[CLOSER] Market closed - deferring {len(should_close)} profit-take orders to next open session (queued market orders can fill badly at the open)")
                for decision in (sorted(should_close, key=lambda x: -x.profit_pct)[:3] if is_market_open else []):
                    try:
                        success = close_position(client, decision.candidate, logger_obj=logger)
                        if success and strat_logger.enabled:
                            strat_logger.log_detailed_trade(
                                {"underlying": decision.candidate.underlying, "symbol": decision.candidate.symbol, "strike": decision.candidate.strike, "dte": decision.candidate.dte, "delta": decision.candidate.delta, "bid_price": decision.candidate.bid, "ask_price": decision.candidate.ask, "oi": None, "contract_type": "put" if decision.candidate.is_put else "call", "underlying_price": decision.candidate.underlying_price, "iv_rank": vol_map.get(decision.candidate.underlying,{}).get("iv_rank") if vol_map else None, "dividend_ex": dividend_map.get(decision.candidate.underlying) if dividend_map else None, "pe": fundamentals_map.get(decision.candidate.underlying,{}).get("data",{}).get("PERatio") if fundamentals_map else None, "profit_dollars_gross": decision.decision_factors.get("profit_dollars_gross", 0), "profit_dollars_net": decision.decision_factors.get("profit_dollars_net", 0), "real_pnl_gross": decision.decision_factors.get("real_pnl_gross", 0), "real_pnl_net": decision.decision_factors.get("real_pnl_net", 0)},
                                score=decision.profit_dollars,
                                decision_type=f"close_{decision.close_type}",
                                market_context=market_ctx
                            )
                    except Exception as e:
                        logger.warning(f"[CLOSER] Close failed for {decision.candidate.symbol}: {e}")
            else:
                near = [d for d in close_decisions if d.profit_pct >= 0.25]
                if near:
                    logger.info(f"[CLOSER] No 50% yet, {len(near)} positions 25%+")
                    for d in sorted(near, key=lambda x: -x.profit_pct)[:5]:
                        logger.info(f"  - {d.candidate.symbol} profit {d.profit_pct:.0%} DTE {d.candidate.dte} needs {(0.5-d.profit_pct)*100:.0f}% more")
                else:
                    logger.info(f"[CLOSER] No positions >=25% profit yet")
        except Exception as e:
            logger.warning(f"Closer eval failed: {e}")

        positions = client.get_positions()
        states = update_state(positions)
        current_risk = calculate_risk(positions)

        # --- Phase 3 Roller v2.5 assignment avoidance debit override ---
        try:
            logger.info(f"[ROLLER] Evaluating rolling need 3% OTM + assignment avoidance debit -$0.20 override, risk ${current_risk:.0f}")
            roll_decisions = evaluate_all_positions(client, config={
                "rolling_otm": adapted.get("ROLLING_OTM", 0.03),
                "dte_critical": 3,
                "delta_threshold": 0.50,
                "loss_threshold": 1.0,
                "profit_threshold": 0.50,
                "min_credit": 0.10,
                "spread_max_abs": adapted.get("SPREAD_MAX_ABS", 0.15),
                "spread_max_pct": adapted.get("SPREAD_MAX_PCT", 0.12),
            })
            strat_logger.log_roll_decisions(roll_decisions)
            need_roll = [d for d in roll_decisions if d.should_roll]
            # v2.6 visibility: surface positions the loss-gate is holding back so the
            # daily review can judge the new trigger, not just count rolls.
            gated = [d for d in roll_decisions if not d.should_roll
                     and any("premium-loss alone" in r for r in d.reasons)]
            for d in gated:
                logger.info(f"[ROLLER] HOLD {d.candidate.symbol}: loss {d.candidate.loss_pct:.0%} but {d.decision_factors.get('otm_pct',0):.1%} OTM delta {d.candidate.delta} - v2.6 gate (would have flagged pre-v2.6)")
            # Prioritize: critical (DTE<=1) first, then high/medium, then nearest expiry.
            # Was evaluation order — in a sell-off the 2-roll-per-run cap could skip the
            # position that actually gets assigned.
            urgency_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            need_roll.sort(key=lambda d: (urgency_rank.get(d.urgency, 3), d.candidate.dte if d.candidate.dte is not None else 999))
            if need_roll:
                logger.info(f"[ROLLER] {len(need_roll)} need rolling:")
                for d in need_roll:
                    logger.info(f"  - {d.candidate.symbol} {d.roll_type} urgency {d.urgency} OTM {d.decision_factors.get('otm_pct',0):.1%} ITM {d.candidate.itm_pct:.1%} DTE {d.candidate.dte}: {d.reasons}")
                if not is_market_open:
                    logger.info(f"[ROLLER] Market closed - deferring {len(need_roll)} rolls to next open session")
                # v2.6: cap defensive rolls per position lineage, then let it ride
                loaded_roll_counts = load_roll_counts()
                roll_counts = prune_roll_counts(loaded_roll_counts, states)
                if roll_counts != loaded_roll_counts:
                    # Pruning was in-memory only until 2026-08-21 — dead
                    # lineages (BAC:P, KO:P) lived in state/roll_counts.json
                    # forever. Persist the prune.
                    save_roll_counts(roll_counts)
                if roll_counts:
                    logger.info(f"[ROLLER] Roll counts: {', '.join(f'{k} {v}/{MAX_ROLLS_PER_LINEAGE}' for k, v in sorted(roll_counts.items()))}")
                capped = []
                for d in need_roll:
                    lineage = f"{d.candidate.underlying}:{'P' if d.candidate.is_put else 'C'}"
                    n = roll_counts.get(lineage, 0)
                    if n >= MAX_ROLLS_PER_LINEAGE:
                        logger.info(f"[ROLLER] {d.candidate.symbol} already rolled {n}x (max {MAX_ROLLS_PER_LINEAGE}) - letting it ride to expiry/assignment (v2.6 cap)")
                    else:
                        capped.append(d)
                if is_market_open and len(capped) > 2:
                    logger.info(f"[ROLLER] Per-run roll cap (2/run): deferring {len(capped) - 2} to next run: " + ", ".join(d.candidate.symbol for d in capped[2:]))
                for decision in (capped[:2] if is_market_open else []):
                    try:
                        underlying = decision.candidate.underlying
                        opt_type = 'put' if decision.candidate.is_put else 'call'
                        lineage = f"{underlying}:{'P' if decision.candidate.is_put else 'C'}"
                        contracts_raw = client.get_options_contracts([underlying], opt_type)
                        snaps = {}
                        occs = [c.symbol for c in contracts_raw]
                        for i in range(0, len(occs), 100):
                            batch = occs[i:i+100]
                            snaps.update(client.get_option_snapshot(batch))
                        avail = []
                        for co in contracts_raw:
                            sn = snaps.get(co.symbol)
                            if not sn:
                                continue
                            try:
                                avail.append(Contract.from_contract_snapshot(co, sn))
                            except Exception as e:
                                logger.debug("[SWALLOWED] roll-target snapshot build failed for %s, skipping contract: %r", getattr(co, 'symbol', '?'), e)
                                continue
                        avail_filtered = filter_options(avail, vol_map=vol_map)
                        targets = find_roll_targets(decision.candidate, avail_filtered, decision, config={
                            "min_credit": -0.20 if decision.urgency=="critical" and decision.candidate.dte<=1 else 0.10,
                            "dte_extension_min": 7,
                            "dte_extension_max": 21,
                            "exp_min": 14,
                            "spread_max_abs": adapted.get("SPREAD_MAX_ABS", 0.15),
                            "spread_max_pct": adapted.get("SPREAD_MAX_PCT", 0.12),
                        })
                        if targets:
                            best = targets[0]
                            logger.info(f"[ROLLER] Rolling {decision.candidate.symbol} -> {best.symbol} net ${best.net_credit:.2f} {best.reasoning} {'(DEBIT AVOID ASSIGNMENT)' if best.net_credit<0 else ''}")
                            success = roll_position(client, decision.candidate, best, logger_obj=logger)
                            if success:
                                roll_counts[lineage] = roll_counts.get(lineage, 0) + 1
                                save_roll_counts(roll_counts)
                            if success and strat_logger.enabled:
                                strat_logger.log_detailed_trade(
                                    {"underlying": underlying, "symbol": best.symbol, "strike": best.strike, "dte": best.dte, "delta": best.delta, "bid_price": best.bid_price, "ask_price": best.ask_price, "oi": best.oi, "contract_type": opt_type, "iv_rank": vol_map.get(underlying,{}).get("iv_rank") if vol_map else None},
                                    score=best.net_credit,
                                    decision_type=f"roll_{decision.roll_type}",
                                    market_context=market_ctx
                                )
                        else:
                            logger.info(f"[ROLLER] No roll targets for {decision.candidate.symbol} {'but critical DTE<=1 will attempt debit roll next cycle' if decision.urgency=='critical' else ''}")
                    except Exception as e:
                        logger.warning(f"[ROLLER] Roll failed for {decision.candidate.symbol}: {e}")
            else:
                logger.info("[ROLLER] No positions need rolling currently >3% OTM safe")
        except Exception as e:
            logger.warning(f"Roll evaluation failed: {e}")

        # Covered calls on existing longs excl SGOV
        for symbol, state in states.items():
            if state["type"] == "long_shares":
                if symbol in TREASURY_SYMBOLS:
                    continue
                # v2.5: Check dividend + earnings for calls too
                sell_calls(client, symbol, state["price"], state["qty"], strat_logger, market_context=market_ctx, dividend_map=dividend_map, execution_config={"limit_enabled": LIMIT_ORDER_ENABLED, "wait_seconds": LIMIT_WAIT_SECONDS})

        positions = client.get_positions()
        states = update_state(positions)
        buying_power = effective_max_risk - calculate_risk(positions)
        # v2.5 options BP check
        acct = client.get_account()
        opt_bp = float(getattr(acct, 'options_buying_power', 0) or 0)
        logger.info(f"[WHEEL] Buying power ${buying_power:.0f} (MAX_RISK ${effective_max_risk} - risk ${calculate_risk(positions):.0f}) Options BP ${opt_bp:.0f} regime {market_ctx.market_regime if market_ctx else 'unknown'} VIX {market_ctx.vix if market_ctx else 'n/a'} IV high {[s for s in vol_report if vol_report[s].get('iv_rank',0)>=50][:3] if vol_report else []}")

        allowed_symbols = [s for s in SYMBOLS if s not in states.keys() and s not in TREASURY_SYMBOLS]
        original_allowed = len(allowed_symbols)
        if earnings_map and EARNINGS_ENABLED:
            from core.earnings_calendar import is_earnings_risk as earnings_risk_check
            from datetime import date
            today = date.today()
            filtered_earn = []
            for s in allowed_symbols:
                blocked, reason = earnings_risk_check(s, earnings_map, today, EARNINGS_BLOCK_DAYS, EARNINGS_BLOCK_DTE)
                if blocked:
                    logger.info(f"[EARNINGS] Skip new CSP {s}: {reason}")
                else:
                    filtered_earn.append(s)
            allowed_symbols = filtered_earn
            logger.info(f"[EARNINGS] Allowed after earnings: {len(allowed_symbols)}/{original_allowed}")
        if fundamentals_map and FUNDAMENTALS_ENABLED:
            filtered_f = []
            for s in allowed_symbols:
                if s.upper() in fundamentals_map and fundamentals_map[s.upper()].get("blocked"):
                    logger.info(f"[FUND] Skip {s}: {fundamentals_map[s.upper()].get('reason')}")
                else:
                    filtered_f.append(s)
            allowed_symbols = filtered_f

        # v2.5.3 sweep model: buying_power based on risk cap (MAX_RISK - risk) matches Fidelity SPAXX where money market counts as collateral
        # Don't block on options_bp if cash+sgov sweep holds collateral earning interest - SPAXX model
        try:
            acct_check = client.get_account()
            cash_check = float(acct_check.cash)
            # SGOV mv already calculated? recalc quick
            sgov_mv_check = 0
            try:
                poss = client.get_positions()
                for pp in poss:
                    if getattr(pp, 'symbol','')=='SGOV':
                        qty = float(getattr(pp,'qty',0) or 0)
                        # Alpaca market_value is already qty*price - do NOT multiply by qty again
                        # (2026-08-20 midday: 492 sh showed total_liq $24.4M instead of ~$102k)
                        mv = float(getattr(pp,'market_value',0) or 0)
                        if mv > 0:
                            sgov_mv_check = mv
                        else:
                            px = float(getattr(pp,'current_price',0) or 0)
                            sgov_mv_check = px * qty if px > 0 else qty * 100.42
            except Exception as e:
                logger.warning("[SWALLOWED] SGOV market-value recheck failed, total_liq may understate: %r", e)
                pass
            total_liq_check = cash_check + sgov_mv_check
        except Exception as e:
            logger.warning("[SWALLOWED] total-liquidity account check failed, treating total_liq/cash as 0: %r", e)
            total_liq_check = 0
            cash_check = 0

        if not is_market_open:
            logger.info(f"[CLOCK] Market CLOSED - skipping new CSP sells (closer/roller already evaluated)")
        elif buying_power >= 2000 and (opt_bp >= 2000 or total_liq_check >= 2000):
            # Fidelity SPAXX: even if opt_bp low after sweep, total liquid (cash+SGOV) still secures puts
            sell_puts(client, allowed_symbols, buying_power, strat_logger, market_context=market_ctx, earnings_map=earnings_map if EARNINGS_ENABLED else None, dividend_map=dividend_map, fundamentals_map=fundamentals_map, vol_map=vol_map, liquidity_map=liquidity_map, execution_config={"limit_enabled": LIMIT_ORDER_ENABLED, "wait_seconds": LIMIT_WAIT_SECONDS}, fund_with_sgov=SGOV_ENABLED and os.getenv("SGOV_FUND_CSP", "true").lower() in ("1", "true", "yes"), rh_feed=rh_feed)
        else:
            logger.info(f"[WHEEL] Insufficient BP stock ${buying_power:.0f} options ${opt_bp:.0f} total_liq ${total_liq_check:.0f} < $2000 min, skipping new CSPs Option A wait - SGOV sweep holds ${total_liq_check:.0f} earning interest")

    if rh_feed is not None:
        try:
            logger.info(rh_feed.summary())
        except Exception as e:
            logger.debug("[SWALLOWED] RH feed summary log failed: %r", e)
            pass
        # 2026-08-18 hardening: cross-check context sources against Robinhood
        # (earnings dates, fundamentals, VIX). Observation only; the engine's
        # own sources remain authoritative for decisions.
        try:
            if earnings_map:
                rh_feed.compare_earnings(
                    {s: str(d) for s, d in earnings_map.items()},
                    days=EARNINGS_CACHE_DAYS if 'EARNINGS_CACHE_DAYS' in dir() else 14)
        except Exception as e:
            logger.debug(f"[RH] earnings cross-check failed: {e}")
        try:
            if fundamentals_map:
                rh_feed.compare_fundamentals(fundamentals_map)
        except Exception as e:
            logger.debug(f"[RH] fundamentals cross-check failed: {e}")
        try:
            eng_vix = getattr(market_ctx, "vix", None)
            rh_feed.compare_vix(eng_vix)
        except Exception as e:
            logger.debug(f"[RH] vix cross-check failed: {e}")

    if SGOV_ENABLED:
        sync_sgov_real(client, logger)
    else:
        logger.info("[SGOV] Sweep disabled (SGOV_ENABLED=False) - cash stays in the broker's own sweep (Robinhood/Fidelity model)")

    if optionable_alive():
        try:
            sync_alpaca_equity_to_optionable(client)
            if SGOV_ENABLED:
                sync_sgov_to_optionable(client)
            sync_closed_trades(client)
            try:
                sync_dividends_and_interest(client)
                sync_option_events(client)
            except Exception as e:
                logger.debug(f"activities sync failed: {e}")
            logger.info(f"Synced positions to Optionable tracker ({len(client.get_positions())} Alpaca positions)")
        except Exception as e:
            logger.warning(f"Optionable sync failed: {e}")
    else:
        logger.warning("Optionable not reachable")

    try:
        strat_logger.save()
    except Exception as e:
        logger.debug(f"Strategy logger save failed: {e}")

    # Push the account snapshot + scan funnel to the Optionable dashboard.
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        _h = int(_dt.now(ZoneInfo("America/New_York")).strftime("%H"))
        _slot = {10: "morning 10:05 ET", 13: "midday 13:05 ET", 15: "afternoon 15:05 ET"}.get(_h, f"run {_h}:05 ET")
        dash_push.push(client, SYMBOLS, locals().get("allowed_symbols", []), slot=_slot)
    except Exception as _e:
        logger.debug(f"dashboard push failed: {_e}")
    finally:
        dash_push.uninstall()

if __name__ == "__main__":
    main()
