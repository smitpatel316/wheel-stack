# Architecture — Unified Wheel Stack

## Overview

Paper-only Options Wheel on Pi budupi $100k, hybrid Model-First LLM+Bayes arXiv:2512.01123 + Sophie quant, agentic via Hermes.

### Components Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│ Hermes Gateway (Telegram)                                           │
│  cron: options-wheel-agentic 5 7,10,12 * * 1-5 ET 10:05/13:05/15:35  │
│  skills: options-wheel-trading, alpaca-mcp                          │
│  MCP: 62 alpaca-mcp + 131 alphavantage SSE                         │
└──────────────┬──────────────────────────────────────────────────────┘
               │ tool_call mcp__alpaca__place_option_order etc
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ core/ Python Strategy Engine ~/wheel-stack/core/                   │
│  Phase 0.1 earnings_calendar.py Finnhub primary + Alpha fallback   │
│  Phase 0.2 dividend_calendar.py Alpha OVERVIEW+DIVIDENDS+Finnhub   │
│  Phase 0.3 fundamentals.py Alpha COMPANY_OVERVIEW P/E Debt/Eq      │
│  Phase 0.4 volatility.py Alpha TIME_SERIES_DAILY 300d RV20d IVrank │
│  Phase 1 context_analyzer.py Yahoo v8 ^VIX real 15.6 fallback      │
│          VIXY*0.6+3.5 calibrated clamp 9-45 adapt_params bear/bull │
│  Phase 2 closer.py Option A 50% DTE>3, 40%+$0.20 DTE7-21, 75% high  │
│  Phase 3 roller.py 3% OTM close-before-open +2s net $0.10 spread   │
│  Phase 4 strategy.py filter_underlying filter_options score/select │
│  Phase 5 execution.py limit mid-price (bid+ask)/2 + market fallback│
│          sgov sweep SPAXX model ideal 1007 $101k $440/mo real 454  │
│  Phase 6 optionable_sync.py + activities_sync.py                   │
└──────────────┬──────────────────────────────────────────────────────┘
               │ REST + MCP
               ▼
┌─────────────┐  ┌──────────────────┐  ┌──────────────────────────┐
│ Alpaca Paper│◄─┤ Broker API       │  │ Alpha Vantage            │
│ $100k PA3WF │  │ IEX feed for bars│  │ EARNINGS_CAL DIVIDENDS  │
│ opt buy 75k │  │ SIP 403 free     │  │ OVERVIEW TIME_SERIES    │
│ stock 350k  │  │ Snapshot batch   │  │ REALTIME_OPTIONS 131    │
└─────────────┘  └──────────────────┘  └──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Optionable v0.16.0 Tracker Docker 8096:8080                         │
│  image yomikoye/optionable React18 Vite Tailwind better-sqlite3 WAL│
│  volumes: optionable-data:/data + /home/.../optionable-data compat │
│  network: wheel-net, health /api/health, tradeCount 15            │
│  API: GET /api/trades envelope {success,data,meta}, POST /api/*   │
│  Sync: push_trade_to_optionable OCC regex AAPL260905P00300000      │
│        delta abs() fix 2026-08-03, commission 0 paper, idempotent  │
└─────────────────────────────────────────────────────────────────────┘
               │ https via cloudflared
               ▼
        wheel.smitpatel.net / optionable alias
        webhook.smitpatel.net -> 8644 finnhub earnings

┌─────────────────────────────────────────────────────────────────────┐
│ Data & Logs                                                          │
│  logs/strategy_log.json 111K, wheel_trades.jsonl 27 factors CPT    │
│  market_context.json 500 ring vix_source spy_5d spy_20d_vol vixy   │
│  earnings_cache.json TTL 6h retain stale 48h on 503, dividend_cache│
│  cron.log quoted heredoc safe <<'LOG' to avoid $0.10 expansion     │
│  optionable.db sqlite trades entryPrice cents closePrice status    │
└─────────────────────────────────────────────────────────────────────┘
```

## Data Flow — One Agentic Tick

1. **Trigger**: Hermes cron `5 7,10,12 * * 1-5` wakes agent with prompt.md 6.5k hybrid
2. **Phase 0 Context**:
   - Earnings: `FINNHUB_API_KEY=... python -c "from core.earnings_calendar import get_earnings_risk_report"`
     - Primary Finnhub `/calendar/earnings?from=&to=&token`, fallback Alpha `EARNINGS_CALENDAR`
     - Cache `logs/earnings_cache.json` TTL 6h, retain stale 48h on 503 (CSCO fix). Block if earnings within 3d or during DTE 21 (NVDA -154k lesson)
   - Dividends: Alpha OVERVIEW ExDivDate + DIVIDENDS + Finnhub stock/dividend, cache 12h, blocks calls ex-div within 2d
   - Fundamentals: Alpha COMPANY_OVERVIEW P/E Debt/Eq yield mkt cap beta, blocks P/E>50 AMD 158.7 SBUX, boost div>1.5% WFC T PG, penalize small cap <$1B 0.85
   - Volatility: Alpha TIME_SERIES_DAILY 300d RV 20d annualized `sqrt(var)*sqrt(252)*100`, RV rank percentile proxy IV rank, high IV>=50 bonus 1.1 low <20 penalty 0.9 adaptive delta

3. **Phase 1 Market Context**:
   - `analyze_context(cli)` → MarketContext
   - Primary Yahoo v8 `https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=1mo&interval=1d` -> closes array last 15.6 real browser verified low -2.44% Aug 3, SPY same endpoint 5d +1.1% realized vol 15.6%
   - Fallback IEX `StockBarsRequest(feed=DataFeed.IEX)` SPY+VIXY daily (SIP 403 free), proxy `VIXY*0.6+3.5=15.62` calibrated was `*1.3+4=30.26` overest 94%
   - `adapt_params(ctx)` → bear 60% risk size10% delta0.25 Mar2020 DD -18.3%, bull 100% 25% 0.35 2021 +45.9%, neutral 75% 15% 0.30 Sophie
   - Save `market_context.json` 500 ring with vix_source/spy_5d/spy_20d_vol/vixy_5d

4. **Phase 2 Closer 50% Option A**:
   - `evaluate_all_for_close(cli)` batched snapshot 100, underlying trades via `get_stock_latest_trade([underlyings])` parsed via `_parse_occ`
   - Triggers: profit >=50% DTE>3 medium, 40%+ $0.20 DTE7-21 low time-efficient, 75%+ high urgency
   - Sort profit descending, max 3/run `close_position()` buy_to_close market side BUY
   - Live Aug 4: INTC260821P77.5 41% $78 profit_take_time FILLED @1.1 entry1.9 $80 42% risk 89.5→81.75k freed 7.75k

5. **Phase 3 Roller 3% OTM**:
   - `evaluate_all_positions(cli)` accurate underlying price, OTM% calc real
   - Flags <3% OTM medium DTE>3, sorting defensive lower strike first net credit desc
   - Roll targets via `find_roll_targets` spread filter abs 0.15 pct 12% NTM 0.05 hard cap 0.30, yield 0.008-0.70 relaxed, delta max 0.45, MIN_PREMIUM 0.20, net credit $0.10 min
   - Execution close-before-open +2s BP free delay fixes 403 insufficient options buying power required 18115 available 14831 on CVX 190→185
   - Max 2/run

6. **Phase 4 Wheel**:
   - MCP `get_watchlist_by_id` wheel-universe 25 → symbols
   - `filter_underlying(client,symbols,BP,earnings_map=build_cache raw Dict[str,date])` NOT risk report
   - `filter_options(options)` OI None allow pass, MIN_PREMIUM 0.20, spread filter, MIN/MAX: MAX_RISK 90k DELTA 0.18-0.35 YIELD 0.008-0.50 EXP 14-60 OI 100 SCORE 0.02
   - `score_options` `(1 - |Δ|) * (250/(DTE+5)) * (bid/strike) * liq_boost 1.1 if OI>500` + spread penalty >5%*0.9 >10%*0.8
   - `select_options` greedy lowest strike within remaining BP
   - Place via MCP `place_option_order` side sell type limit at mid `(bid+ask)/2` 8s wait market fallback day sell_to_open `wheel-{ticker}-{strike}-{YYYYMMDD}-1`
   - BP guard $2000 min Option A wait, duplicate guard via `get_orders` OPEN, one per underlying <10%
   - Push to Optionable `push_trade_to_optionable`

7. **Phase 5 SGOV v2.5.3 Sweep**:
   - `cash = acct.cash`, `sgov_mv = qty*price`, `total_liquid = cash+sgov_mv`
   - `target_ideal = total_liquid -500` → Fidelity SPAXX ideal 1007 shares $101k $440/mo $5,281/yr 5.22%
   - `max_sgov_affordable = stockBP -1k`, `target_real = min(ideal, affordable+mv)` → Alpaca real 454 $45k $198/mo due to 40310000 stock not cash collateral
   - Execution `place_sgov_limit_order` limit +1c improvement not market via MCP `place_stock_order`
   - BP check for new puts: `buying_power>=2000 AND (opt_bp>=2000 OR total_liquid>=2000)` SPAXX model
   - Sync `sync_sgov_to_optionable(client)` single arg

8. **Phase 6 Sync & Reporting**:
   - Optionable health `/api/health` v0.16.0 tradeCount 15 open 12 closed 3
   - `sync_closed_trades(client)` compares Alpaca OCCs vs Optionable Open, marks Expired if exp<=today else Assigned if stock exists else Closed via PUT /api/trades/{id} — NOTE needs closePrice fix for P/L bug
   - Activities via MCP `get_account_activities_by_type` DIV INT FEE OPASN OPEXP
   - Logging `app_logging/strategy_logger.py` v2 27 factors: `wheel_trades.jsonl` one per trade trade_type (roll_defensive/new_put/close_profit_take_50) score contract enriched otm_pct itm_pct spread_pct premium_rate ann_yield assignment_prob_est market_regime vix_level volatility bn_reasoning, close_decisions, roll_decisions, market_context flatten
   - Report: equity/cash/P/L, regime VIX source adapted MAX_RISK, BP risk SGOV qty, closer profit$, roller flagged symbols credit, puts placed, earnings blocked CSCO NVDA dividends found AAPL F XOM fundamentals blocked AMD SBUX high IV AAPL..., Optionable count, MCP tools used, next theta decay 5-8 days closer trigger

## Model-First Hybrid LLM+Bayes

From arXiv:2512.01123 + Sophie AI quant framework (references/hybrid-llm-bayesian-wheel-2025.md, quant-framework-sophie-ai.md):

- LLM as builder not trader: strategy code generated/adapted, not per-trade LLM call (cost+latency)
- Bayesian Network nodes: market_regime (bear/bull/neutral), volatility (high/med/low IV rank), technical (SPY trend 5d, vol), earnings risk, dividend risk, fundamentals, liquidity
- Edges: VIX→regime→DELTA_MAX/RISK/SIZE, earnings→block, dividend→early assignment, IV rank→adaptive delta max favorable high IV bonus 1.1, fundamentals→score boost/penalty
- Dynamic adaptation: low VIX 15.6 → neutral medium balanced 30-45DTE 0.30Δ size15% MAX_RISK 90k full; high VIX 30+ → bear high 0.25Δ risk60% size10% assign15% Mar2020 DD -18.3% case
- 27 factors logged per trade for CPT (Closer Profit Tracker) future model training after 100+ trades
- Paper: 8919 trades 15.3% Sharpe 1.08 DD -8.2% 0% assign 371% roll rate large universe

## Safety Invariants

- PAPER ONLY IS_PAPER=true never flip without explicit permission
- NEVER 0DTE EXP_MIN 14 (gamma + 3:30pm auto-liquidate)
- MAX_RISK 90k allows 10-12 puts diversified (was 75k blocked CAT 81.4k), raised for rolling per paper 10-25% size, live 12 puts $81.25k fully utilized 90.8% BP $8.75k
- One per underlying, never >10% per name, ask if MAX_RISK>100k
- SGOV excluded from risk, Treasury symbols excluded
- Duplicate OPEN order guard, never margin, backup cron 2am cp fallback sqlite3 missing handled
- System crontab 2 jobs only cloudflared watchdog + backup, Hermes cron 2 active tamelabs every 4h + wheel 3/day, alpaca-stream.service stopped/disabled

## Deployment Mapping

- `docker-compose.yml` root unified: optionable service volume `optionable-data:/data` legacy `/home/smitpatel316/optionable-data` compat, network `wheel-net`, healthcheck, optional wheel-runner profile manual
- `hermes/cron/` contains agentic prompt + README install instructions
- `hermes/mcp/` alpaca.json 62 tools + alphavantage.json 131 tools + README gateway restart guard
- `pi/deploy.sh` sg docker compose up -d, cloudflared ingress check, hermes cron list, backup instructions
- `pi/cloudflared-config-snippet.yml` ingress wheel.smitpatel.net -> 8096, webhook.smitpatel.net -> 8644
- `docs/` architecture.md, pnl-fix.md, improvements-roadmap.md, deployment.md

## Future Tracks

- Optionable P/L fix closePrice=0 $568 vs $52 bug -> sync_closed_trades must write actual closePrice from Alpaca fills
- True P/L reconciliation via Alpaca activities FILL buy_to_close vs sell_to_open
- Limit order mid-price execution improvement tracking 8s wait market fallback slippage 0.15% saved
- Robinhood MCP long-only limitation workaround: official https://agent.robinhood.com/mcp/trading only long equities/options orders, wheel needs short puts — stay Alpaca until they add short
- Automated testing, monitoring, alerting
