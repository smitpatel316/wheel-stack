# Wheel Stack v2.6.0 — Unified Options Wheel + Optionable + Hermes Agentic

**Private repo merging:** `~/options-wheel` Python wheel engine + `Optionable` tracker (wheel.smitpatel.net:8096 @ yomikoye/optionable:latest) + Hermes agentic setup (cron 014708b33a6a + skill options-wheel-trading + Alpaca MCP 62 tools + AlphaVantage 131 tools)

Paper $100k PA3WFOAHE2C6, hybrid v2.5.3/v2.5.4 production, model-first: Earnings+Dividend+Fundamentals+Volatility Context -> Closer 50% -> Roller 3% -> Wheel -> SGOV SPAXX sweep -> Optionable

## Why Unified?

Before: 3 repos / locations fragmented:
- `~/options-wheel` core logic, but .env keys scattered, logs in logs/, params.py v2.4
- `/home/smitpatel316/optionable-data` docker-compose + DB 15 trades
- `~/.hermes/skills/pi/options-wheel-trading` 53k SKILL.md + 30 references, cron prompt 20k chars in ~/.hermes/cron/jobs.json, MCP config in gateway

Now: One repo `~/wheel-stack` with all three working hand-in-hand, so changes propagate.

## Architecture

```
wheel-stack/
├── config/
│   ├── params.py          # MAX_RISK 90k, DELTA 0.18-0.35 adaptive, YIELD 0.008-0.50, EXP 14-60, OI 100 allow None, spread $0.15/12% NTM $0.05, closer 50% DTE>3, roller 3% OTM
│   ├── symbol_list.txt    # 25 diversified: AAPL CSCO INTC AMD BAC WFC F T VZ SBUX KO ... SPY
│   ├── credentials.py     # IS_PAPER=true, loads .env
│   ├── webhook_config.json # finnhub earnings webhook public_url secret
│   └── .env.example       # ALPACA_API_KEY, FINNHUB, ALPHA, OPTIONABLE_URL
├── core/
│   ├── strategy.py        # filter_underlying, filter_options OI None fix, score_options liq boost spread penalty, select_options greedy lowest strike within BP
│   ├── execution.py       # sell_puts/calls, push to Optionable, limit mid-price logic
│   ├── roller.py          # v2.5 debit -$0.20 DTE<=1, 3% OTM, close-before-open 2s BP free, spread $0.15/12% $0.30 cap, max 2/run
│   ├── closer.py          # v2.3 Option A 50% DTE>3 profit_take_50, 40%+$0.20 DTE7-21 time-efficient, 75% high urgency, max 3/run
│   ├── context_analyzer.py # MarketContext Yahoo v8 ^VIX real 15.6 primary, VIXY*0.6+3.5 calibrated clamp 9-45, SPY 5d +1.1% vol 15.6%
│   ├── earnings_calendar.py # Finnhub + Alpha EARNINGS_CALENDAR fallback, cache 6h, block 3d + DTE21 (NVDA Jun -$154k lesson), webhook clears cache
│   ├── dividend_calendar.py # Alpha OVERVIEW ExDiv + DIVIDENDS, cache 12h, blocks calls ex-div 2d/DTE
│   ├── fundamentals.py    # Alpha COMPANY_OVERVIEW P/E Debt/Eq div yield mkt cap beta, blocks P/E>50 AMD 158, boost div>1.5%
│   ├── volatility.py      # Alpha TIME_SERIES_DAILY RV 20d annualized, RV rank IV proxy high>=50 bonus 1.1 low<20 penalty 0.9 adaptive delta
│   ├── liquidity.py       # Spread filter + volume OI trend
│   ├── optionable_sync.py # Optionable ↔ wheel bridge — fetches real closePrice from Alpaca fills, syncs trades
│   ├── pnl_tracker.py     # True P/L reconciliation Alpaca fills vs Optionable
│   └── broker_client.py   # MarketBuy/Sell via Alpaca-py, IEX feed for free tier SIP 403
├── app_logging/
│   └── strategy_logger.py # 30 factors: wheel_trades.jsonl, market_context 500 ring, roll/close decisions, real PnL
├── scripts/
│   ├── run_strategy.py    # CLI main hybrid phases 0.1-6 with IEX context
│   └── agentic_runner.py  # NEW hermes wrapper using MCP where possible
├── optionable/
│   ├── docker-compose.yml # optionable:8096 + wheel-api optional 8097
│   └── data/              # gitignored, host /home/.../optionable-data
├── hermes/
│   ├── skill/
│   │   ├── SKILL.md       # 53k production skill
│   │   └── references/    # 30 md - hybrid-v2.2, v2.5.3 sweep, RH MCP, etc.
│   ├── cron/
│   │   ├── options-wheel-agentic.prompt.md # 20k full prompt from 014708b33a6a
│   │   └── README.md      # install instructions
│   └── mcp/
│       ├── alpaca.json    # uvx alpaca-mcp-server 62 tools
│       ├── alphavantage.json # 131 tools
│       └── README.md      # MCP everywhere principle + gateway guard
├── pi/
│   ├── deploy.sh          # pi deployment + verification
│   └── cloudflared-config-snippet.yml # wheel.smitpatel.net -> 8096, webhook -> 8644
├── docs/
│   ├── architecture.md    # full system diagram + data flow
│   ├── pnl-fix.md         # P/L reconciliation details
│   ├── improvements-roadmap.md # v2.5.3, v2.5.4, upcoming
│   └── deployment.md      # pi steps
├── docker-compose.yml     # root unified compose (optionable + optional api)
├── Dockerfile.wheel-api   # optional FastAPI for status
├── .gitignore
├── pyproject.toml
└── README.md
```

## Hermes Agentic Setup (MCP Everywhere)

User mandate 2026-08-02: "remove redundant cron jobs and use mcp everywhere where possible and not re-implement api where not needed. all our trading will be agentic"

- System crontab 2 jobs only: cloudflared watchdog + backup 2am
- Hermes cron 2 jobs: tamelabs every 4h (paused now) + options-wheel-agentic 5 7,10,12 * * 1-5 PDT (ET 10:05/13:05/15:35) id 014708b33a6a
- MCP servers: alpaca-mcp 62 tools (get_account_info, get_clock, get_all_positions, get_orders, place_option_order, get_account_activities_by_type DIV/INT/FEE/OPASN/OPEXP, etc) + alphavantage 131 tools (EARNINGS_CALENDAR, DIVIDENDS, COMPANY_OVERVIEW, TIME_SERIES_DAILY) via https://mcp.alphavantage.co
- Custom kept: Optionable REST (no MCP) + strategy scoring (filter/score/select) + roller/closer/context (model-first hybrid arXiv:2512.01123)
- Gateway restart guard: `hermes gateway restart` blocked from inside gateway process safety anti-loop id #30719 - must SSH outside `systemctl --user restart hermes-gateway.service`
- Execution: scan via Python broker_client scoring, place via MCP place_option_order market/limit mid-price, guard duplicate via get_orders OPEN, SGOV sweep via place_stock_order, Optionable sync via custom REST

Prompt lives at `hermes/cron/options-wheel-agentic.prompt.md` (20k chars), install via `hermes cronjob create`.

## Production Params v2.5.3 + v2.5.4

From `config/params.py`:
```
MAX_RISK = 90_000 (was 75k blocked CAT 81.4k, raised for diversified 12 puts $81.25k live + rolling)
DELTA_MIN = 0.18 / DELTA_MAX = 0.35 adaptive via IV rank (VIX>25 or IVRank>50 -> 0.20 conservative bear)
YIELD_MIN = 0.008 / MAX 0.50 (was 0.06 blocking 10-40% real)
EXPIRATION 14-60 (never 0, gamma + 3:30pm liquidate)
OPEN_INTEREST_MIN 100 allow None (Alpaca 2262/5132 None, was 500 blocking 40%)
SCORE_MIN 0.02
MIN_PREMIUM 0.20
SPREAD_MAX_ABS 0.15 / PCT 0.12 / NTM $0.05 (Sophie 10% $0.05 non-negotiable, blocks MP 40P $0.61 25%)
ROLLING_OTM 0.03 (was 0.05 too sensitive flagged 4/5 day1, now 1/9)
MIN_CREDIT 0.10 roll net credit floor, DTE_CRITICAL 3, DELTA_THRESHOLD 0.50
CLOSER 50% DTE>3 profit_take_50, 40%+$0.20 DTE7-21 time-efficient redeploy, 75% high urgency
VIX Yahoo v8 chart ^VIX primary real 15.6 browser verified, VIXY*0.6+3.5=15.62 calibrated clamp 9-45
Earnings: Finnhub + Alpha fallback, cache 6h, block 3d + during DTE21 (NVDA Jun -$154k bag), 1 blocked CSCO 2026-08-19, NVDA 08-26
Dividend: Alpha OVERVIEW ExDiv + DIVIDENDS, cache 12h, blocks calls ex-div 2d/DTE early assignment risk, found AAPL 08-10, F 08-11, XOM 08-17
Fundamentals: P/E Debt/Eq div yield mcap beta, blocks extreme P/E>50 AMD 158 SBUX, boost div>1.5% WFC T PG
Volatility: RV 20d annualized RV rank proxy for IV rank, high IV>=50 bonus 1.1, low<20 penalty 0.9, adaptive delta, high IV AAPL/CSCO/INTC/AMD/BAC/WFC/F/T
Execution: limit at mid (bid+ask)/2 8s wait market fallback cuts slippage, place_limit_or_market_sell
SGOV: SPAXX/RH sweep model v2.5.3 - ideal Fidelity 1007 shares $101k $440/mo 5.22% vs real Alpaca 454 $45k $198/mo vs old 104 $10.5k $45/mo due to Alpaca stockBP limit 40310000, SGOV is stock not cash collateral
```

## Quickstart (Pi budupi)

```bash
cd ~/wheel-stack
cp config/.env.example .env
# edit .env with ALPACA_API_KEY (paper), FINNHUB, ALPHA_VANTAGE

# Deploy Optionable
./pi/deploy.sh
# or sg docker -c 'docker compose up -d optionable'
curl http://localhost:8096/api/health

# Verify P/L fix
python3 -c "from core.pnl_tracker import reconcile_optionable_vs_alpaca; from config.credentials import *; from core.broker_client import BrokerClient; c=BrokerClient(ALPACA_API_KEY,ALPACA_SECRET_KEY,IS_PAPER); print(reconcile_optionable_vs_alpaca(c))"

# Install hermes cron (requires hermes agent)
hermes cronjob create --schedule "5 7,10,12 * * 1-5" --name options-wheel-agentic --skills options-wheel-trading,alpaca-mcp --prompt "$(cat hermes/cron/options-wheel-agentic.prompt.md)"
hermes cronjob list

# Cloudflare tunnel 2-step
# 1. ingress in ~/.cloudflared/config.yml:
#   - hostname: wheel.smitpatel.net
#     service: http://localhost:8096
# 2. DNS:
#   cloudflared tunnel route dns pi-tunnel wheel.smitpatel.net
```

## Improvements Roadmap

- **v2.5.3** SGOV SPAXX sweep + Robinhood official MCP https://agent.robinhood.com/mcp/trading (long-only limitation, wheel needs short puts - stay Alpaca for now)
- **v2.5.4** Closer profit-take + SGOV full sweep 688 shares + earnings map 503 fallback bug fix
- **v2.6.0** Unified repo + P/L fix real closePrice + pnl_tracker + hermes agentic layer docker unified + docs (this release)
- **Next:** 
  - Automated tests for roller/closer/spread filter (prevent MP $0.61 25% re-entry)
  - True P/L reconciliation cron job nightly writes discrepancy alert if inflated >$50
  - Limit order execution improvement tracking - log mid-price vs fill slippage
  - Robinhood MCP A/B test when they add short puts: compare native interest vs SGOV wrapper
  - Monitoring: webhook for closer profit-take + roller defensive alerts to Telegram
  - Earnings webhook Finnhub real-time already working https://webhook.smitpatel.net/webhooks/finnhub-earnings X-Finnhub-Secret

## Safety

- PAPER ONLY IS_PAPER=true, never flip without explicit permission
- One contract per symbol, <10% per name, ask before MAX_RISK>80k
- NEVER EXP_MIN=0 (0DTE 3:30pm auto-liquidate)
- Always --strat-log, 30 factors logged
- SGOV Treasury symbols excluded from risk
- Commission 0 paper, guard duplicate open orders

## Links

- Optionable: https://wheel.smitpatel.net (local http://localhost:8096) v0.16.0 React18+Vite Recharts Express better-sqlite3 WAL
- Pi: budupi, Docker, cloudflared tunnel pi-tunnel b826..., compose /data/docker/compose
- Hermes: default profile, telegram delivery origin, cron 2 jobs
- Alpaca: Paper $100k PA3WFOAHE2C6 level3 multiplier 4x equity $99k P/L -$176 day1 spread decay

## License

MIT - original options-wheel by Alpaca, enhancements Smit Patel 2026
