# Wheel Hybrid v2 Started 2026-08-03 — Rolling + Context Analyzer + 27 Factors

Extends wheel-started-2026-08-03.md

## Commit
- 287ad55 feat(wheel): hybrid architecture v2 - model-first LLM-as-builder + rolling engine + 27-factor logging
- 7 files changed, 1492 insertions
- Templates synced params_prod.py 90k

## New Modules Implemented (Paper arXiv:2512.01123)

### core/roller.py — 0% assignment target via aggressive rolling
Paper: 5803 rolls from 1563 puts (371%) -> 0 assigned, Sharpe 1.08 DD -8.2% vs -60% QQQ.
Dataclasses: RollCandidate (occ underlying strike exp dte qty entry/current delta bid/ask underlying_price itm_pct loss_pct profit_pct is_put), RollTarget (symbol strike exp dte bid/ask delta oi premium_rate ann_yield net_credit roll_type reasoning), RollDecision (should_roll roll_type defensive/offensive/assignment_avoidance/profit_take urgency low/med/high/critical reasons decision_factors).

evaluate_roll_need(candidate, config):
- OTM pct = (underlying-strike)/strike for puts, if < rolling_otm 0.05 -> should_roll defensive medium (high if ITM <0)
- DTE <= dte_critical 3 and near ITM -> assignment_avoidance critical
- delta abs > delta_threshold 0.50 -> defensive high if >0.60
- loss_pct > loss_threshold 1.0 -> defensive medium
- profit_pct >= profit_threshold 0.50 and DTE>7 -> profit_take low optional (Reddit trader early close style)
Returns decision_factors 27-style.

find_roll_targets(candidate, available_contracts, decision, config):
- Requires DTE extend +7 min +21 max (+30 extra flexible for critical), EXP_MIN 14 never 0DTE
- Strike: defensive puts same/lower, offensive higher, assignment same ±5%
- Delta 0.18-0.45 (relaxed from 0.35+0.10), premium MIN 0.20, yield 0.008-0.70 relaxed for rolls
- Net credit = new_bid - close_cost (current_price) >= min_credit 0.10
- Sort: defensive lower strike first higher credit, offensive higher net.
- Returns top 5.

roll_position(client, candidate, target, logger): buy_to_close old via MarketOrderRequest BUY qty abs, sell_to_open new via SELL qty abs, push to Optionable. Uses TradeClient directly (MCP equivalent would be place_option_order buy/sell).

evaluate_all_positions(client, config): get_positions filter OCC via parse_option_symbol, batch get_option_snapshot 100, underlying trades, build candidates via build_roll_candidate_from_position (parses OCC, gets underlying price, current, entry, greeks delta, quote bid/ask, calc itm _calc_itm_pct, loss/profit). Returns decisions.

### core/context_analyzer.py — LLM-as-model-builder
Paper model-first: LLM constructs BN not trades.

MarketContext dataclass: timestamp, vix, vix_level low/med/high/extreme, market_regime bull/neutral/bear, trend up/down/neutral/choppy, volatility_level high/med/low, iv_rank_avg, symbols_analyzed, avg_daily_vol_ok, fomo/confidence/stress/tilt_risk 0-1, technical_position oversold/neutral/overbought, spy_price, spy_change_pct, underlying_momentum dict, bn_nodes/edges/reasoning, decision_factors dict.

Functions:
- _classify_vix(vix): <15 low, <25 med, <35 high else extreme
- _classify_regime(spy_5d, vix_level): >3% + low/med = bull, <-3% or high/extreme = bear else neutral
- _classify_technical(avg_mom): >5% overbought <-5% oversold
- get_vix_and_spy(client): try client.stock_client latest trade SPY, fallback Yahoo query1.finance.yahoo.com chart ^VIX and SPY range 5d (currently 403 blocked → vix None, regime neutral). Logs debug.
- analyze_context(client, symbols, use_llm=False): builds context from vix/spy, momentum placeholder, psychological defaults (bull fomo 0.7 conf 0.8 stress0.2, bear fomo0.1 conf0.4 stress0.7 tilt0.3), BN hypotheses per regime: bear/high vol -> nodes include VIX, FOMO, Stress, Position Size, edges Vol→Strike, Vol→AssignProb, Regime→Size, reasoning March 2020 adaptive vol emphasis assign 15% size 10%, decision_factors regime/vix/recommended_size_pct/assignment_prob_est/case_study. Bull -> size 25% 8% OTM aggressive. Neutral -> 15% 5% balanced Sophie. If use_llm and OPENAI_API_KEY -> _enrich_with_llm.
- _enrich_with_llm(ctx, market_data): POST {base_url}/chat/completions model gpt-4o-mini temp0.1 max_tokens 1500 system You are BN expert Output ONLY JSON, user includes VIX, SPY, regime, trend, vol, technical, FOMO/confidence/stress/tilt, params. Regex extract JSON nodes edges reasoning recommended_params -> merge into ctx.
- adapt_params(ctx, base): bear/high -> DELTA_MAX 0.25 MIN 0.15 EXP_MAX45 MIN14 MAX_RISK 60% (45k if 75k base or 60% of current 90k = 54k) etc, bull -> 0.35 14-60 MAX_RISK 90k 100% size25% ROLLING_OTM 0.08 aggressive, neutral -> 0.30 14-45 75% size15% 0.05. VIX low -> YIELD_MIN 0.015 require higher, extreme -> DELTA_MAX min 0.20 MAX_RISK 50% cut. Returns dict overrides with NOTE.
- save_context_log(ctx, path logs/market_context.json): append to list keep last 500.

Live: first run vix None level medium neutral, 25 symbols_analyzed, bn_reasoning "Neutral regime, VIX medium, balanced approach 30-45 DTE 0.30 delta per Sophie + paper balanced", adapted NOTE same size 15% delta max 0.3 risk 75000 (base param used, not yet updated to 90k in adapt fallback — fix TODO use current config). Second run identical.

### app_logging/strategy_logger.py v2 — 27 factors
Paper: 27 decision factors per trade fully explainable.

FACTOR_CATEGORIES dict market_regime, volatility, option_fundamentals, premium, risk, position, decision. ALL_27_FACTORS list.

StrategyLogger:
- __init__ enabled log_path strategy_log.json jsonl_path wheel_trades.jsonl, parent mkdir, version hybrid-v2-27factors.
- set_market_context(market_context): to_dict if needed, store log_entry market_context dict + flatten regime/vix/vix_level/volatility_level/technical_position/bn_nodes/edges/reasoning.
- log_roll_decisions(roll_decisions): serialize candidate.symbol underlying strike dte should_roll roll_type urgency reasons decision_factors itm_pct loss_pct profit_pct.
- _enrich_contract_dict(contract_dict, market_context): calc otm_pct itm_pct based on type put/call, spread_pct (ask-bid)/mid, premium_rate bid/strike, ann_yield bid/strike*365/(dte+1), assignment_prob abs(delta) or 0.25, enriched includes delta_abs theta gamma vega iv volume enriched_at via get_ny_timestamp, merges market_context regime/vix_level/volatility/technical.
- log_detailed_trade(contract_dict, score, decision_type, market_context): mc_dict to_dict, enriched via _enrich, jsonl_entry timestamp trade_type new_put/roll_defensive/etc score contract enriched flatten underlying symbol strike dte delta delta_abs bid ask spread oi otm itm premium_rate ann_yield assignment_prob market_regime vix vix_level volatility technical theta gamma vega iv buying_power risk_exposure bn_reasoning. Append to JSONL file one line, also log_entry detailed_trades list. Errors to logging_errors list not crash.
- save(): load existing list, append, keep last 1000 entries, write indent2.

Result: logs/wheel_trades.jsonl 6 lines after 2 runs, logs/market_context.json 2 entries, logs/strategy_log.json 111K includes market_context bn_nodes/edges roll_decisions detailed_trades.

### core/execution.py — market_context plumbing
sell_puts(client, allowed_symbols, buying_power, strat_logger, market_context): filter_underlying, get_options_contracts put, get_option_snapshot batch, Contract.from_contract_snapshot, filter_options, strat_logger.log_put_options, set_market_context if provided, score_options, select_options, for each selected need 100*strike filter buying_power, market_sell, _push_wheeler 404 ignore, push_trade_to_optionable with delta getter, log_sold_puts, log_detailed_trade with score decision_type new_put market_context.

sell_calls similar with market_context.

### scripts/run_strategy.py — 6 phases
1 Context: analyze_context(use_llm=False), adapt_params, set_market_context, save_context_log, log regime/vix/adapted NOTE.
2 Roller: evaluate_all_positions with adapted ROLLING_OTM dte_critical 3 delta 0.50 loss 1.0 profit 0.50 min_credit 0.10, log_roll_decisions, need_roll = should_roll, profit_take = roll_type profit_take. For each need_roll[:2] get contracts for underlying puts/calls, snapshots batch 100, build avail list via Contract.from, filter via filter_options, find_roll_targets, if targets best net credit -> roll_position, log_detailed_trade roll_{type}, else log No roll targets.
3 Covered calls: states long_shares excl TREASURY, sell_calls with market_context
4 Puts: buying_power = effective_max_risk - risk, allowed = SYMBOLS not in states not TREASURY, sell_puts with market_context
5 SGOV sync real via client
6 Optionable sync via alive etc + activities_sync DIV/INT/FEE/OPASN/OPEXP + logger save.

effective_max_risk = adapted MAX_RISK or base MAX_RISK 75k->90k.

## Bug Fixes Same Day

### Optionable delta 400
POST /api/trades with delta -0.296 -> {"error":"delta must be between 0 and 1"}. Alpaca puts delta negative, calls positive, but Optionable schema 0-1. Fix: delta_val = abs(float(delta)) in push_trade_to_optionable line 103-104. After fix pushes succeed with abs: INTC 0.1821 MP 0.3073 CSCO 0.2777 XOM 0.3199 BAC 0.3104 CVX 0.34. Health tradeCount 6->11.

### Template double-write
config/params.py had duplicate content due to sed -i double apply: first line MAX_RISK 75k kept plus 90k added at line 11 duplicated entire file. Fix: clean_params.py rewrite single clean file with 90k + rolling constants. Copied to templates/params_prod.py.

### MAX_RISK raise
Risk after 4 new CSPs 54k + existing 5 = 54.25k, after CVX 73.25k, remaining BP 1.75k tight. Original limit 75k too small for 25 tickers with expensive SPY 74.6k CAT 81.4k. Paper case studies position size 10-25% per name, 19y $100k->1.44M, monthly 1.19% premium, 475 trades/yr. To allow diversified 10 puts need >70k. Raised to 90k (20% increase) allowing CAT, still ask if >100k per safety rule updated in skill. With 90k remaining 35.75k after 10 puts actually risk 73k remaining 17k but Alpaca options buying power 5304 after large notional because cash-secured put requires strike*100 cash reserved not buying_power, but options_buying_power 26651 after earlier trades — tight. Might need 95k later.

## Live Execution Details

### Pre-hybrid (11:44 PDT manual)
5 CSPs: F14 0.24, T22.5 0.24, PFE24.5 0.33, VZ46 0.49, BAC61 0.66 all 18D 2026-08-21, risk 16.8k premium 196, SGOV 497->828.

### Hybrid v2 Run1 (12:18 PDT)
Context neutral, adapted 0.3 delta 75k (old adapt not reading new 90k). Roller 4 need rolling medium OTM<5%: BAC 1.7% F 3.8% PFE1.6% VZ3.4%. Rolled BAC61->60 Sep18 net $0.38 credit: close 0.69 fill, open 1.05 fill, Optionable logged CSP BAC 60 Sep18 $1.07. No targets for F. Buys: INTC 77.5 $1.88 (score 0.237), AMD 430 $26.17 fails insufficient BP required 40285 avail 26651, MP 40 $2.12 score0.208, CSCO108 $2.56 score0.186, HON typo HON2? 230 $10.94 not tradable, XOM150 $2.45 score0.132, CVX190 fails BP, DLR fail, WFC fail etc. SGOV 828->455 sell 373 FILLED $100.42, sync Optionable BAC 61 Closed trade13, trades 10 + SGOV 455. wheel_trades.jsonl 5 lines: roll_defensive BAC + 4 new_puts.

### Hybrid v2 Run2 (12:19 PDT)
Context same neutral. Roller 5 need rolling medium: BAC Sep 3.4% F3.7% PFE1.6% VZ3.3% XOM3.1%. No roll targets meeting net credit (checked 2). Buys: CVX190 $3.12 score0.129 -> Optionable logged, others skipped BP $1.75k, SGOV 455->266 sell 189, risk $73.25k SGOV 266 BP tight.

## Positions Final (as of last check)
- BAC260918P00060000 -1 1.05 1.09 -4
- CSCO260821P00108000 -1 2.59 2.96 -37
- CVX260821P00190000 -1 3.1 3.35 -25 (new run2)
- F260821P00014000 -1 0.24 0.29 -5
- INTC260821P00077500 -1 1.9 2 -10
- MP260911P00040000 -1 2.14 2.74 -60
- PFE260821P00024500 -1 0.33 0.37 -4
- SGOV 266 100.43 100.425 immaterial
- T260821P00022500 -1 0.24 0.22 +2
- VZ260821P00046000 -1 0.49 0.52 -3
- XOM260821P00150000 -1 2.4 2.54 -14
risk $73.25k, SGOV 266x = $26.7k cash equivalent, remaining BP $26.7k idle calc but Alpaca options BP 5304 due to cash-secured.

## Files
- logs/wheel_trades.jsonl 8.6K 6 lines sample:
{"timestamp":"2026-08-03T12:18:05...","trade_type":"roll_defensive","score":0.38,"contract":{...premium_rate 0.0178 ann_yield 0.138 spread 0.036 assignment 0.31 market_regime neutral vix_level medium...}, "symbol":"BAC260918P00060000", "strike":60, "dte":46, ... bn_reasoning:"Neutral regime..."}
- logs/market_context.json 3.1K 2 entries bn_nodes 8 edges 7 decision_factors regime neutral vix medium recommended_size_pct 15 assignment_prob_est 0.05
- logs/strategy_log.json 111K includes market_context roll_decisions detailed_trades
- Optionable health v0.16.0 tradeCount 11, trades API 11 entries id 9-18 active + id13 closed.

## Next Steps / TODO from session
- [ ] VIX fetch fix: Yahoo 403, replace with Alpaca options implied or CBOE index via data feed or polygon. Currently vix None -> medium fallback.
- [ ] redo adapt_params to read current config MAX_RISK 90k not hardcoded 75k inside context analyzer (uses adapt returning 75k currently but effective_max_risk uses config, so OK but note mix)
- [ ] Spread filter: Sophie < $0.05 NTM non-negotiable, add filter_options check latest_quote bid/ask spread <0.10 or <10% mid skip
- [ ] Roller sensitivity: 5% OTM too aggressive same-day flagging, consider 3% or only critical DTE<=3
- [ ] LLM enrichment: set OPENAI_API_KEY and use_llm=True in run_strategy for real BN generation
- [ ] CPT building: need 100+ trades to start populating Bayesian tables like paper 8919 trades 252-day window
- [ ] Agentic cron prompt updated 6.2k chars hybrid v2, next auto runs 7:05/10:05/12:35 PDT will use new logic
- [ ] Check backup cron sqlite3 missing (pre-existing)
- [ ] Verify SGOV monthly div sync via activities_sync DIV will push to fund
