# Wheel Started 2026-08-03 11:44 PDT — First Live CSPs After Filter Fix

## Summary
- Market: OPEN is_open=true, next_close 16:00 ET, equity $99,995, buying_power $340k, options $75k, cash $50k
- Before: SGOV 497x100.425 only, risk $0, 0 puts due to YIELD_MAX 0.06 + OI None blocking
- After fix: 5132 contracts scanned, 3326 with delta, OI with 2870 None 2262, filtered 174, selected 23 best per underlying
- Placed 5 CSPs via MCP place_option_order market sell_to_open, all FILLED:
  - F260821P00014000 $0.24 x1 FILLED 2026-08-21 exp delta -0.296 OI 6787 risk $1400
  - T260821P00022500 $0.24 x1 FILLED 22.5 delta -0.265 OI 725 risk $2250
  - PFE260821P00024500 $0.33 x1 FILLED 24.5 delta -0.346 OI 10488 risk $2450
  - VZ260821P00046000 $0.49 x1 FILLED 46 delta -0.292 OI 526 risk $4600
  - BAC260821P00061000 $0.66 x1 FILLED 61 delta -0.338 OI 1513 risk $6100
- Total put risk $16,800, premium collected $196 (100*1.96), remaining BP $58,200
- SGOV: 497->828 (+331) @100.43 FILLED via place_stock_order, idle target $83,200
- Positions now: SGOV 828, 5 short puts mkt -$214, commission 0 paper
- Optionable: health v0.16.0 tradeCount 5, trades id 9-13 CSP Open, SGOV stock 828 id 14, pushes via push_trade_to_optionable

## Params Fixed
config/params.py:
- DELTA_MAX 0.30->0.35 widened
- YIELD_MAX 0.06->0.50 (real market 10-40% annualized, formula (bid/strike)*365/(dte+1))
- EXP_MAX 45->60
- OPEN_INTEREST_MIN 500->100 + allow None as pass
- MIN_PREMIUM 0.20 added

core/strategy.py:
- filter_options: allow OI None as pass `if oi is not None and oi < MIN`, premium guard bid>=0.20, robust _calc_yield
- score_options: liq boost 1.1 if OI>500, try/except robust

## Execution Pattern Proven
1. get_account_info, get_clock, get_all_positions, get_orders, get_watchlists wheel-universe 25
2. Python strategy: filter_underlying by buying power, get_options_contracts put, get_option_snapshot batch 100, Contract.from_contract_snapshot, filter_options, score_options, select_options greedy by strike asc within remaining BP
3. Place via MCP: place_option_order symbol OCC qty 1 side sell type market position_intent sell_to_open client_order_id wheel-{sym}-{strike}-{date}
4. After puts: calculate_exposures -> idle=TOTAL-risk, target_shares=floor(idle/price), diff, place_stock_order SGOV diff buy/sell market day, guard duplicate open orders via get_orders open
5. Optionable sync: push_trade_to_optionable for each new trade, sync_sgov_to_optionable, sync_alpaca_equity_to_optionable via OPTIONABLE_URL=http://localhost:8096
6. Verify: curl /api/health, /api/trades, /api/stocks
7. Log to logs/cron.log

## Cron Hygiene
- System crontab -l 2 jobs only ✓ cloudflared watchdog + backup 2am
- Hermes cron list 2 active ✓ tamelabs 240m, options-wheel-agentic 5 7,10,12 * * 1-5 PDT
- alpaca-stream.service inactive dead disabled since Aug 2 22:05 — already stopped per agentic migration, no TradingStream. MCP polling replaces it.

## Portfolio History
get_portfolio_history 1M 1D base 100k, last 2 days flat.

## MCP Tools Used This Run
get_account_info $100k equity buying_power 340089 options 75040 PA3WFOAHE2C6 level3 multiplier 4x
get_clock is_open true next_open Mon 09:30 ET next_close 16:00
get_all_positions SGOV 497 -> 828 after
get_orders open/closed, place_option_order x5 FILLED, place_stock_order SGOV +331 FILLED
get_watchlists wheel-universe 25 id 40cc59d4
get_stock_latest_trade for price feed

## Next Steps
- Next auto runs 10:05, 12:35 PDT will see risk $16.8k remaining $58.2k and scan for more candidates
- Monitor fills, assignments, expirations via get_account_activities_by_type DIV/INT/FEE/OPASN/OPEXP
- Optionable will track RoR, monthly P/L once trades close
