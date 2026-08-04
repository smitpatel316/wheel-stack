# Agentic Migration 2026-08-02 — MCP Everywhere

## Before (redundant cron + custom REST)
System crontab 5 jobs: cloudflared watchdog + 3x run_wheel_cron.sh (7:05,10:05,12:35 PDT) + backup 2am
Hermes cron 2 one-shots options-wheel-debug 06:45 PDT, market-open 07:10
Services: alpaca-stream.service TradingStream wss://paper-api.alpaca.markets/stream + alpaca-activities.service SSE 404
Re-implementations: broker_client.py market_buy/sell/get_account, activities_sync.py raw REST DIV/INT/OPASN, is_market_open.py GET /v2/clock, sync_watchlist.py POST /v2/watchlists, alpaca_stream_sync.py, alpaca_activities_sse.py

## After (agentic)
System crontab 2 jobs only: cloudflared watchdog 5min + backup 2am gzip
Hermes cron 2 jobs: tamelabs every 4h + options-wheel-agentic 5 7,10,12 * * 1-5 PDT Mon-Fri uses MCP 62 tools (skills: options-wheel-trading + alpaca-mcp)
Services: optionable container 8096 healthy Up, hermes-gateway with mcp_stdio_watchdog uvx alpaca-mcp-server 62 tools ✓ enabled
Archived deprecated/ folder: old stream scripts moved, run_wheel_cron.sh now DEPRECATED stub

## MCP tools replacing custom code
- is_market_open.py -> mcp_alpaca_get_clock is_open guard, skip wheel when closed but still SGOV+Optionable
- sync_watchlist.py -> mcp_alpaca_get_watchlists / create_watchlist wheel-universe 25 symbols
- activities_sync.py -> mcp_alpaca_get_account_activities_by_type DIV INT FEE OPASN OPEXP JNLS TRANS
- broker_client market_buy/sell -> mcp_alpaca_place_stock_order / place_option_order
- alpaca_stream_sync.py -> agentic job uses get_orders + get_account_activities polling (MCP REST-only, no websocket)

## Kept custom (not in MCP)
- core/optionable_sync.py POST /api/trades OCC parser AAPL260905P00300000 -> ticker, type CSP/CC, strike, exp, premium, idempotent DELETE before POST, sync_closed_trades Assigned/Expired
- core/strategy.py filter_underlying, filter_options, score_options, select_options delta 0.18-0.30 yield 0.008-0.06 exp 14-45 OI 500 SCORE_MIN 0.02 (custom wheel logic)
- Optionable tracker React18 has no MCP, needs custom REST

## Safety unchanged
PAPER ONLY IS_PAPER true, MAX_RISK 75k TREASURY_SYMBOLS SGOV/USFR/BIL/SHV/TFLO excluded, one contract/symbol, ask before >80k, NEVER 0DTE EXP_MIN 14, SGOV real buy guard duplicate open orders via get_orders OPEN filter

## Verification
hermes mcp list alpaca ✓ enabled 62 tools, hermes mcp test alpaca ✓ Connected 2149ms, gateway Main PID 2633546 restarted 21:58 with watchdog, tool_search shows mcp__alpaca__* live, get_account_info equity 100k buying_power 350k options 75k, get_clock is_open false next_open Mon 09:30 ET, get_orders SGOV buy 496 accepted queued, watchlists wheel-universe 25.

## Lesson: use MCP everywhere where possible, do not re-implement trading-api.json
When official MCP server exists (alpacahq/alpaca-mcp-server v2.2.0 FastMCP from OpenAPI 203KB 40 paths), use it for all Alpaca reads/writes instead of custom broker_client. Only keep custom for Optionable + strategy scoring.
