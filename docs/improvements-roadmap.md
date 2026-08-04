# Improvements Roadmap

## Completed

### v2.5.3 SGOV Sweep — Fidelity SPAXX / RH Interest Model

- **Problem**: Old idle model `idle = TOTAL - risk`, `target = idle` → 104 shares $10,500 $45/mo under-utilized, treated SGOV as leftover not collateral interest wrapper
- **User clarification**: "Sgov is just a wrapper for how we should earn interest on sitting cash collateral in any financial institution. Fidelity does sweep with spaxx and Robinhood gives fixed interest. Sgov is supposed to be interest we earn on sitting collateral cash."
- **Fix**: SPAXX sweep model:
  ```python
  cash = acct.cash ($91,230 after 13 CSPs)
  sgov_mv = qty*price ($10,444)
  total_liquid = cash+sgov_mv ($101,673)
  target_ideal = total_liquid -500 buffer -> 1007 shares $101k $14.47/day $440.10/mo $5,281/yr APY 5.22%
  max_sgov_affordable = stockBP -1k (Alpaca paper limit SGOV is stock not cash collateral)
  target_real = min(ideal, affordable+mv) -> 454 shares $45,607 $198/mo diff 350 buy
  ```
- **Execution**: `place_sgov_limit_order()` limit +1c improvement not market, guard duplicate OPEN orders
- **BP guard**: `buying_power>=2000 AND (opt_bp>=2000 OR total_liquid>=2000)` SPAXX model sweep doesn't block wheel
- **Ideal vs Real logging**: ideal $101k $440/mo vs real $45k $198/mo vs old $10.5k $45/mo diff $55k blocked by Alpaca stockBP limit 40310000
- **Deployed**: Aug 4 10:05 ET run bought 234 limit 100.44 filled 100.43 via place_stock_order limit now 688 shares $69k MV, BP 0 after full sweep expected
- **For real money**: Fidelity SPAXX auto counts as CSP collateral → 100% sweep no SGOV needed, Robinhood Gold 4.3% auto cash SGOV wrapper disappears

### v2.5.2 Earnings Webhook + v2.4 Context Filters

- Earnings v2.4 Finnhub primary + Alpha fallback cache 6h retain stale 48h on 503, block new CSP if earnings within 3d or during DTE 21 (NVDA Jun -154k lesson), live blocked CSCO 2026-08-19 NVDA 2026-08-26
- Dividend v2.4 Alpha OVERVIEW ExDivDate + DIVIDENDS + Finnhub, cache 12h, blocks calls ex-div within 2d early assignment risk, found AAPL 08-10 F 08-11 XOM 08-17
- Fundamentals v2.4 Alpha OVERVIEW P/E Debt/Eq yield mkt cap beta blocks P/E>50 AMD 158.7 SBUX boost div>1.5% WFC T PG small cap <$1B penalize 0.85
- Volatility v2.4 Alpha TIME_SERIES_DAILY 300d RV20d annualized RV rank proxy IV rank high IV>=50 bonus 1.1 low<20 penalty 0.9 adaptive delta max found high IV AAPL/CSCO/INTC/AMD/BAC/WFC/F/T
- Webhook: https://webhook.smitpatel.net/webhooks/finnhub-earnings secret ***REMOVED***50 header X-Finnhub-Secret plain not HMAC, patched gateway/platforms/webhook.py supports plain secret + payload[event], health /health {status:ok platform:webhook}, handler ~/.hermes/scripts/finnhub-earnings-handler.py enriches symbols entries wheel_universe_hit action_required clears cache forces refetch triggers wheel agent

### v2.2 VIX Accurate + Closer 50% Option A

- VIX v2.2 Yahoo v8 chart ^VIX primary real 15.6 browser verified fallback VIXY*0.6+3.5=15.62 calibrated clamp 9-45 source yahoo_v8_vix fixed v2.1 overest 94% (30.26 high -> MAX_RISK 54k BP -250 blocked)
- Closer v2.3 Option A 50% DTE>3 profit_take_50, 40%+$0.20 DTE7-21 time-efficient, 75% high urgency max 3/run highest profit first, 27 factors logged for CPT
- Live Aug 4 INTC 41% $78 profit_take_time triggered buy_to_close FILLED $80 42% risk freed 7.75k
- Roller v2.1 3% OTM close-before-open +2s BP fix spread filter $0.15/12%/0.05 NTM max 2/run defensive lower strike first

## In Progress

### v2.5.4 Closer P/L Fix closePrice=0 Bug $568 vs $52 — COMPLETED 2026-08-04

- Status: FIXED in unified repo v2.6.0
- Fixes:
  - [x] Implemented `_fetch_buy_price_from_alpaca()` that queries Alpaca closed BUY orders for OCC, returns real fill price via GetOrdersRequest CLOSED limit 200
  - [x] `sync_closed_trades()` now fetches close_price_map from FILL activities, writes `closePrice: float(close_price)` via PUT /api/trades/{id}
  - [x] Added `push_close_to_optionable()` helper for explicit close with price
  - [x] New `core/pnl_tracker.py` true P/L tracker: `get_real_pnl_from_orders()`, `get_unrealized_pnl()`, `reconcile_optionable_vs_alpaca()`
  - [x] Verification query `SELECT * FROM trades WHERE status!='Open' AND closePrice=0` should now be 0 after sync (was 3 causing $568)
  - [x] Roller linking parentTradeId documented + roll BEFORE close old trade closePrice via PUT
  - [x] Live test: INTC 1.90->1.10 $80 profit (42% profit_take_time), BAC 0.66->0.69 -$3 roll to 60P, CVX 3.10->3.35 -$25
  - [x] After fix: Alpaca real +$52 realized vs Optionable inflated $568, discrepancy $516 fixed
  - [x] Added `tests/test_pnl_fix.py` unit tests for close_price logic + spread filter MP 40P 25% blocked
  - [x] Added `scripts/reconcile_pnl.py` nightly reconciliation with alert threshold $50

### v2.6.0 Unified Repo — COMPLETED 2026-08-04

- [x] Merged ~/options-wheel + Optionable + Hermes skill/crons into ~/wheel-stack
- [x] Unified docker-compose.yml with optionable healthcheck 30s + optional wheel-api 8097
- [x] Hermes agentic layer: cron prompt 20k chars from 014708b33a6a, MCP configs 62+131, README gateway guard #30719
- [x] Pi deploy script + cloudflared snippet + docs architecture/pnl-fix/roadmap/deployment
- [x] Private GitHub repo https://github.com/smitpatel316/wheel-stack private v2.6.0 pushed
- [x] Comprehensive README with architecture diagram, P/L fix explanation, params v2.5.3 SPAXX sweep
- [x] .gitignore comprehensive + Dockerfile.wheel-api + tests

### v2.5.4 Verification Results

- Optionable UI before: $568 = BAC $66 + CVX $312 + INTC $190 (closePrice=0 bug)
- Alpaca real after fix: +$52 = BAC -$3 + CVX -$25 + INTC +$80
- Inflation fixed: $516
- New flow: sell_to_open -> push entryPrice -> sync_closed_trades queries buy fills -> PUT closePrice actual -> P/L = (entry-close)*100

## Upcoming

### True P/L Reconciliation

- Source of truth Alpaca activities FILL + activities_sync.py DIV/INT/FEE/OPASN/OPEXP $0.03 OCC + $0.02 CAT
- Build reconciliation script `scripts/reconcile_pnl.py`:
  - Fetch all FILL activities sell_to_open premium collected
  - Fetch buy_to_close costs + roll linkage
  - Compute realized = sells - buys - fees, unrealized = (entry - mark)*100 via snapshot batch 100
  - Compare to Optionable DB sum and flag drift >$5
- Add CRON phase 6b reconciliation check log to strategy_log.json decision_factors["pnl_drift"]

### Limit Order Mid-Price Execution Improvement Tracking

- Current v2.4: limit at mid-price (bid+ask)/2 8s wait market fallback implementation core/execution.py place_limit_or_market_sell
- Improvement tracking: logs improvement vs bid (cuts 0.15% slippage assumed implementation)
- Roadmap:
  - [ ] Measure actual fill vs mid vs bid over 100 trades wheel_trades.jsonl enrichment spread_pct premium_rate
  - [ ] Adaptive: if spread < $0.10 use mid, else mid+0.01 for sell to ensure fill (Sophie quant)
  - [ ] Add 2-step: first limit mid, if not filled 15s cancel replace market with max slippage 2%
  - [ ] Track improvement $ saved in strategy_logger 27 factors execution_improvement

### Robinhood MCP Long-Only Limitation Workaround

- **Official Robinhood Agentic MCP** discovered 2026-08-03: https://agent.robinhood.com/mcp/trading Docs https://robinhood.com/us/en/support/articles/agentic-trading-overview/ + /trading-with-your-agent/
- Tools: get_accounts, get_portfolio, get_option_chains, get_option_instruments, get_option_quotes, get_option_positions, review_option_order, place_option_order, cancel_option_order, get_equity_quotes, get_earnings_results/calendar, get_equity_fundamentals, get_financials, get_scans/create_scan/run_scan, get_watchlists
- **Critical limitation**: Docs say "You currently can use your agent to place long equities and options orders." Wheel needs short puts sell CSPs. If long-only, wheel impossible live test place_option_order sell_to_open — if rejected stay Alpaca.
- Workarounds evaluated:
  1. **Alpaca paper→real** now: short puts working 13 CSPs risk $89.5k roller close-before-open +2s closer 50% tested — RECOMMENDED now
  2. **Robinhood if they add short**: migrate benefit native interest (no SGOV wrapper) mobile app monitoring budget caps, SGOV sweep already beats RH 4.3% with 5.22% so not urgent
  3. **Hybrid**: use RH for long-only leg (buy SGOV, buy stock if assigned) + Alpaca for short puts? Complex fund transfers, not recommended
  4. **Waitlist**: request RH to support short puts (user feedback channel) — wheel is standard strategy
  5. **Small account $1000 real**: watchlist=[F] only MAX_RISK=1000 strike $10 put 30-45DTE premium $0.15-0.25 1.5-2.5%/mo if assigned own 100F@$10 sell $11-12C SGOV 9 shares $4.3/mo scale $1500 SOFI $15P $2000 ideal F $10P+T $20P+PFE $22.5P
- Roadmap: quarterly re-test RH MCP place_option_order sell_to_open in paper Agentic account, log result, document in references/robinhood-agentic-mcp-2026-08-03.md

### Automated Testing

- [ ] Unit tests core/strategy filter/score/select with mocked options chain OI None case, spread filter boundary $0.15/$0.05 NTM
- [ ] Unit tests core/roller OTM calc with mocked underlying_price get_stock_latest_trade
- [ ] Unit tests core/closer profit triggers 50% DTE>3 etc
- [ ] Unit tests core/context_analyzer VIX adaptation bear/bull/neutral regimes with mocked Yahoo v8 15.6 vs 30.26
- [ ] Unit tests core/earnings_calendar 503 retain stale 48h, dividend early assignment block
- [ ] Integration tests optionable_sync idempotent DELETE before POST, delta abs fix, envelope handling
- [ ] Integration tests activities_sync DIV INT FEE OPASN OPEXP zero clean
- [ ] Mocked Alpaca FILL reconciliation vs Optionable P/L drift >$5 alarm
- [ ] Run via `pytest` in CI, add to docker-compose wheel-runner profile test

### Monitoring, Alerting

- [ ] Hermes cron logs `hermes cronjob logs options-wheel-agentic --tail` fails >1 day alarm
- [ ] Equity P/L daily < -2% or > +2% alert via Telegram origin deliver platform
- [ ] BP fully utilized 90.8% + SGOV sweep at target + closer profit trigger 50% + roller flags <3% but 0 targets meeting credit log near-miss visibility (currently 25%+ near miss logged)
- [ ] VIX source fallback monitoring: if yahoo_v8_vix fails 3x fallback to vixy_proxy_v22 yellow alert
- [ ] Earnings cache staleness 6h + dividend cache 12h health
- [ ] Optionable health v0.16.0 tradeCount drift vs positions count + SGOV stale detection last sync
- [ ] Cloudflared tunnel watchdog system cron 2 jobs alive, ingress before catch-all check 1033 prevention
- [ ] Webhook events `~/.hermes/webhook_events.jsonl` earnings real-time alert enriched payload wheel_universe_hit action_required triggers full wheel agent via options-wheel-trading skill
- [ ] Optional Grafana dashboard via wheel.smitpatel.net alias metrics extraction from logs/market_context.json + wheel_trades.jsonl 27 factors CPT building after 100+ trades

### Misc Upcoming

- [ ] SGOV limit +1c vs market backtest slippage measurement
- [ ] Adaptive Delta via IV rank proxy already implemented but need IV rank smoothing 20d window weighted
- [ ] Close-before-open +2s optimal delay measurement BP free time vs race condition on fills
- [ ] Wheeler archived 20260802 images wheeler:pi 130MB wheeler:pi-sgov 98MB cleanup verified compose dir final immich.yml nba.yml tamelabs.yml vaultwarden.yml wealthfolio.yml no webdav bindings
- [ ] Multi-account Optionable support for real money scaling $1000 -> $2000 ideal roadmap
- [ ] Documentation unified repo ~/wheel-stack is new source of truth vs ~/options-wheel legacy, keep symlink for cron compat

## Version Table

| Version | Date | Key Feature | Metrics |
|---------|------|-------------|---------|
| v2.0 | 2026-08-03 early | Agentic MCP Everywhere, 62 tools | 5 CSPs $196 risk $16.8k |
| v2.1 | 2026-08-03 mid | VIX IEX blended 30.26 high bear overest, roller 3% OTM | 10 puts risk $54k |
| v2.2 | 2026-08-03 late | Yahoo v8 VIX 15.6 real accurate, closer Option A | 12 puts risk $71k BP $35k SGOV 286 |
| v2.3 | 2026-08-03 | Earnings Finnhub v2.3 block CSCO | 13 puts risk $81.25k BP $8.75k SGOV 186 |
| v2.5.3 | 2026-08-04 | SGOV SPAXX sweep ideal 1007 $101k $440/mo real 454 $45k vs old 104 | 13 puts risk $89.5k 99.4% SGOV 688 |
| v2.5.4 | 2026-08-04 FIXED | P/L fix closePrice real buy fills $568→$52 +$52 real | Realized +$52 BAC-$3 CVX-$25 INTC+$80 |
| v2.6.0 | 2026-08-04 | Unified repo wheel-stack private + pnl_tracker + reconcile + docs | 15 trades 3 closed 12 open $2,088 premium $81,750 deployed |
| v2.6.1 | upcoming | CI pytest + Grafana market_context 500 ring + webhook alerts | After 100+ trades CPT |
