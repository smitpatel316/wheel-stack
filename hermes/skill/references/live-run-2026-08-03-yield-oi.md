# Live Agentic Run 2026-08-03 10:05 ET — Yield + OI Blocker

## Run Summary
- Clock MCP: is_open=true, next_close 16:00 ET
- Account: equity $99,997.52, buying_power $340k, options $75k, cash $50k
- Positions: SGOV 496 -> 497 after +1 buy via MCP place_stock_order
- Watchlist: wheel-universe 25 OK id 40cc59d4
- Wheel: filter_underlying 23/25 PASS, get_options_contracts 4087 puts, snapshots 4087, filter_options -> 0
- Activities DIV/INT/FEE/OPASN/OPEXP/OPEXC all 0 (fresh paper)
- Portfolio history base 100k flat
- Optionable: SGOV 496 x100.72 -> 497 x100.43 after sync, trades 0, health v0.16.0
- SGOV order: 1 share market day placed via MCP mcp__alpaca__place_stock_order qty 1 side buy, filled pending_new -> 497, open orders guard checked 0 before
- Cron: system 2 jobs only (cloudflared + backup), hermes 2 jobs (tamelabs 240m + options-wheel-agentic 5 7,10,12 * * 1-5 PDT), alpaca-stream.service inactive dead disabled

## Yield Blocker Discovery
YIELD_MAX=0.06 = 6% annualized blocks almost all CSPs in live market:

Sample delta 0.18-0.30 puts (2026-08-03 live):
- AAPL 290P 14D bid 1.79 yield 15.02%
- CSCO 103P 18D bid 1.61 yield 30.03%
- BAC 60P 18D bid 0.41 yield 13.13%
- WFC 83P 18D 0.57 yield 13.19%
- F 14P 18D 0.18 yield 24.7%
- T 22.5P 18D 0.19 yield 16.22%
- VZ 45.5P 18D 0.33 yield 13.93%
- SBUX 100P 18D 0.63 yield 12.1%
All >6% filtered out.

Even low premium: WFC 80P 39D 0.34 yield 3.88% passes, but most wheel $0.30-$1.50 on $20-$60 = 10-40% annualized.
Formula: (bid/strike)*(365/(dte+1))
$0.50 on $50 30D -> (0.5/50)*365/31 = 11.77% -> blocked
$0.40 on $100 30D -> 4.7% passes but rare.

Loose debug YIELD 0.001-1.00 needed to see candidates. Prod 0.008-0.06 too tight.

## OI Blocker Discovery
Alpaca Trading API returns open_interest=None for many newer expirations:
- T260911P00022500 OI=None exp 2026-09-11
- AAPL 290P 14D OI None
- CSCO 103P 18D OI 278 but many 32D+ expirations None
- WFC 80P 39D OI None

Filter requires oi>500, so even if yield passes, OI None fails.

In earlier test: 262 contracts F,BAC total, only 145 have OI non-None. For T 121 total, many None.

MCP get_option_chain also returns snapshots without OI in some cases (greeks present but OI from contract endpoint).

## Decision
- Do NOT place 0 puts this run — safe default.
- SGOV + Optionable sync still executed (closed market path works).
- Recommend for next iteration: raise YIELD_MAX to 0.50-1.00 OR switch to absolute premium filter ($0.30 min), and allow OI None with fallback to snapshot or lower threshold OI 100 / allow None.

## Commands Used
```bash
cd ~/options-wheel && source .venv/bin/activate
python -c "from core.strategy import filter_options..."
# via MCP:
mcp__alpaca__get_clock
mcp__alpaca__get_account_info
mcp__alpaca__get_all_positions
mcp__alpaca__get_orders open limit 50
mcp__alpaca__get_watchlist_by_id
mcp__alpaca__get_option_chain underlying F expiration_gte 2026-08-17 lte 2026-09-17 type put limit 100
mcp__alpaca__get_stock_latest_trade SGOV
mcp__alpaca__get_account_activities_by_type DIV/INT/FEE/OPASN/OPEXP/OPEXC
mcp__alpaca__place_stock_order SGOV 1 buy market day
mcp__alpaca__get_portfolio_history period 1M timeframe 1D
```

## Follow-up
- Update config/params.py if approved: YIELD_MAX 0.06 -> 0.50
- Update filter_options to allow oi None or fallback
- Update skill docs filter-debugging with new yield distribution
- Verify next run Mon 10:05 PDT still 0 puts expected until params loosened
