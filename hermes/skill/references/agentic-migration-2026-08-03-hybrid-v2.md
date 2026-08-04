# Agentic Migration 2026-08-03 — Hybrid v2 Update

## Prompt v2 6.2k chars (job 014708b33a6a)

Updated 2026-08-03 after hybrid architecture implementation.

### Phase 1 — Context (Model-First Paper)
Run: `cd ~/options-wheel && ~/options-wheel/.venv/bin/python -c "from core.context_analyzer import analyze_context, adapt_params; ctx=analyze_context(use_llm=False); print(ctx.to_dict()); print(adapt_params(ctx))"`
- MarketContext: regime bull/neutral/bear, vix_level low/med/high/extreme, volatility_level, technical oversold/neutral/overbought, BN nodes/edges/reasoning, decision_factors roll 27 factors, FOMO/confidence/stress/tilt 0-1
- Adapt: bear/high vol -> DELTA_MAX 0.25 MAX_RISK 60% size 10% assignment 15% Mar2020 case, bull -> 0.35 100% 25% 8% OTM 2021 case, neutral -> 0.30 75% 15% Sophie balanced
- Log to logs/market_context.json ring 500 for CPT

### Phase 2 — Roller (0% assignment target)
- evaluate_all_positions(client, config={rolling_otm 0.05, dte_critical 3, delta 0.50, loss 1.0, profit 0.50, min_credit 0.10})
- Triggers OTM<5% buffer, DTE<=3 near ITM critical, delta>0.50, loss>100% defensive, profit>=50% profit_take optional
- find_roll_targets(candidate, avail, decision): net_credit >=0.10 DTE +7-21 strike logic defensive lower/same offensive higher assignment same ±5% delta 0.18-0.45 premium 0.20 yield 0.008-0.70, sort net credit, top5
- roll_position: buy_to_close old + sell_to_open new via MCP place_option_order buy/sell market wheel-roll-{underlying}-{date}
- Paper 5803 rolls /0 assigned
- Log to wheel_trades.jsonl 27 factors

### Phase 3 — Wheel sells
Allowed = 25 minus state minus treasury
Python strategy filter_underlying get_options_contracts 5132 snapshots batch100 filter_options 174 score/select 23 greedy lowest strike within BP
MCP place_option_order sell market sell_to_open wheel-{sym}-{strike}-{date}
Safety dup via get_orders OPEN, skip SGOV CCs, ask if MAX_RISK>100k

### Phase 4 — SGOV
idle=TOTAL-risk target=idle/price diff via place_stock_order guard dup

### Phase 5 — Optionable sync
`OPTIONABLE_URL=http://localhost:8096 python -c "from config.credentials import *; from core.broker_client import BrokerClient; c=BrokerClient(API,SECRET,IS_PAPER); from core.optionable_sync import *; sync_alpaca_equity_to_optionable(c); sync_sgov_to_optionable(c); sync_closed_trades(c)"`
Verify curl /api/health /api/trades
Fix delta abs() expects 0-1 Alpaca -0.3 puts -> abs

### Phase 6 — Activities + Logging
MCP get_account_activities_by_type DIV/INT/FEE/OPASN/OPEXP -> fund, get_portfolio_history
Log 27 factors wheel_trades.jsonl strategy_log.json market_context.json cron.log

Report: market open/closed, regime VIX level adapted NOTE, buying_power risk, rolls old->new net credit, profit_take candidates, puts selected, fills, SGOV qty, Optionable count, MCP tools used, 27 factors.

### Schedule
ET 10:05am 1:05pm 3:35pm = PDT 7:05 10:05 12:35 M-F, replaces old crons. System cron only cloudflared watchdog + backup 2am. alpaca-stream dead MCP polling.

### First live hybrid v2
- 11:44 PDT 5 CSPs $196 premium risk 16.8k SGOV 497->828 Optionable 5
- 12:18 PDT context neutral medium, 4 rolling need medium, rolled BAC61->60 Sep18 $0.38 credit, + INTC/MP/CSCO/XOM risk 54.25k SGOV 455 Optionable 11
- delta abs fix pushes 5 OK, tradeCount 11
- Next 12:19 CVX added risk 73.25k SGOV 266

### Steps to update prompt in future
`cat ~/.hermes/cron/jobs.json | python3 -c "import json,pathlib; data=json.loads(pathlib.Path.home().joinpath('.hermes/cron/jobs.json').read_text()); job=[j for j in data['jobs'] if j['id']=='014708b33a6a'][0]; job['prompt']='NEW PROMPT'; pathlib.Path.home().joinpath('.hermes/cron/jobs.json').write_text(json.dumps(data,indent=2))"`
