# Hybrid LLM + Bayesian Network for Wheel — Paper Analysis

Source: A_Hybrid_Architecture_for_Options_Wheel_Strategy_D.pdf (arXiv:2512.01123v1, Kuang & Lin, Nov 30 2025, 11 pages)

## Abstract Summary
LLMs good at context/qualitative nuance, bad at rigorous transparent reasoning. Model-first hybrid: LLM as intelligent model builder, not black-box decision-maker. For each trade, LLM constructs context-specific Bayesian Network interpreting market conditions (prices, vol, trends, news) and hypothesizing relationships. LLM selects relevant historical data from 18.75y 8,919-trade dataset to populate CPTs focusing on analogous scenarios. BN does transparent probabilistic inference → explicit distributions + risk metrics. Feedback loop: LLM analyzes outcomes, refines structure + data selection.

Results 19y out-of-sample: 15.3% ann return, Sharpe 1.08 vs 0.62 benchmarks, drawdown -8.2% vs -60% market, 0% assignment via strategic rolling, 27 factors/trade avg, fully explainable.

## Architecture (4 components, Table 1)
- Context Analyzer: LLM-based market interpretation, news, technical
- Network Constructor: LLM-based variable ID, causal mapping, structure gen
- Probability Engine: Data-driven historical querying, CPT population
- Inference Module: Belief propagation, variable elimination, decision optimization

## Why LLMs Fail in Finance (paper's critique)
- Hallucination in numeric reasoning (compound probs, EV, risk)
- Opacity: billions params black box no audit
- Stochastic inconsistency: same market → different recs
- Probability miscalibration: unjustified confidence
- Lack causal understanding: correlation vs causation

## Contribution: Model-First (LLM-Augmented Probabilistic Reasoning)
- Dynamic BN per decision, nodes = market vars edges = causal links
- LLM provides rationale for design
- First application LLM-guided BN in algorithmic trading
- Intelligent data selection: LLM picks relevant historical periods (vol regime etc) not all data or manual window → CPTs contextually appropriate
- Feedback for continuous learning: closed-loop evolves capturing patterns distinguishing success/fail
- Empirical: on thousands live trades spanning 19y

## BN Variables (Table 2)
Market Regime (Bull/Neutral/Bear), Volatility Level (High/Med/Low), Stock Fundamentals (Strong/Mod/Weak), Technical Position (Oversold/Neutral/Overbought), Strike Selection (Conservative/Moderate/Aggressive), Premium Rate (High/Med/Low), Assignment Probability (High/Med/Low), Trade Outcome (Profit/Breakeven/Loss)

## Example CPTs (Table 3)
Bear+Conservative=0.02 assign prob, Bear+Moderate=0.08, Bear+Agg=0.25, Neutral+Conservative=0.01, Neutral+Mod=0.05, Bull+Conservative=0.005

## Dataset & Validation Protocol
- $100k initial, 2007-Sep 2025, 8,919 trades, multi high-vol instruments leveraged ETFs TQQQ SOXL UPRO TECL FAS + mega-cap NVDA GOOGL AMZN TSLA
- Wheel: sell puts 10% OTM, hold assigned, sell calls at assignment price until called away
- 27 decision factors per trade (vol assessment, OTM%, premium rates, risk levels, sizing, market context, rationale)
- Temporal: Train 2007-2015 (inc 2008 crash), Val 2016-2019 (2018 correction), Test 2020-Sep 2025 (COVID crash recovery volatility) — strict out-of-sample
- Look-ahead prevention: any trade t uses data <= t-1, rolling 252d window, retrain every 6mo walk-forward simulates live
- Costs: $0.65/contract + $0.10 exchange fee min $1/trade → $12,518 total, slippage 0.15% puts 0.12% calls, position size max 5% ADV multi-day execution if larger, transaction costs reduce ret 0.3pp net 15.0% after costs vs 15.3% gross

## Performance (Table 4-9)
- Summary 2007-2025: Avg Ann 15.3%, Final $1.44M from $100k, Total Premium $1.91M, 475 trades/yr, Avg $214/trade, Put trades sold 1,563 expired 1,553 (99.4%) rolled 5,803 (371.3%) assigned 0 (0%), Avg premium rate 11.15%, Avg monthly 1.19%, Winning years 19/19 100%, Factors/trade 27
- Year-by-year: 2007 14% vs SPY 5.3% QQQ 18.8% 489 trades, 2008 18.6% vs -36.2% -40.8% 498 trades (bear outperformance), 2009 9.3% vs 22.7% 48.3%, 2010 4.3% vs 13.1% 18.4% 1096 trades, 2011 5.3% vs 0.9% 1.9%, 2012 4.9%, 2013 7.2%, 2014 8.9%, 2015 10.6% vs 1.3% 9.8%, 2016 9.9%, 2017 11.5%, 2018 24.1% vs -5.2% -1.8% (strong bear), 2019 16.2%, 2020 26.6% vs 17.2% 46.0%, 2021 45.9% vs 28.7% 27.4% $332k premium bull opt, 2022 27.4% vs -18.2% -32.6% bear massive outperformance, 2023 14.4%, 2024 23.5%, 2025 12.6% Sep only
- Baseline comparison (Table 6): Hybrid 15.3% Sharpe 1.08 DD -8.2% 8919 trades 19/19 consistency, Pure LLM 8.7% 0.45 -28.3% 3247 trades 15/19, Static BN 11.2% 0.67 -18.9% 4156 17/19, Rules 9.8% 0.52 -22.1% 3891 16/19, SPY 11.27% 0.55 -55% 16/19, QQQ 17.53% 0.62 -60% 16/19
- Economic risk-adjusted (Table 7) vs QQQ: Ann 15.3 vs 17.53, Sharpe 1.08 vs 0.62, Sortino 1.45 vs 0.78, Max DD -8.2 vs -60, Avg DD -2.1 vs -8.7, VaR95 -3.2 vs -12.8, ES -4.1 vs -18.2, Calmar 1.87 vs 0.29, CRRA certainty equivalent 12.8-14.1% vs 8.2-11.3% prefers hybrid for risk-averse
- ETF baseline 2020-2025 (Table 8): QYLD 6.61% Sharpe 0.45 Sortino 0.46 DD -24.75%, PUTW 9.03% 0.65 0.66 -28.4% — hybrid superior
- With CIs (Table 9): Ann 15.3% [13.8,16.8], Monthly 1.19% [1.05,1.33], Sharpe 1.08 [0.9,1.3], DD -8.2% [-10.1,-6.3], Win 99.4% [99.2,99.6], Premium rate 11.15% [10.8,11.5], Rolling 371% [365,378]
- Stat sig t-test (Table 10): vs Pure LLM +0.55% t4.23 p<0.001 sig, vs Static BN +0.34% p0.004 sig, vs Rules +0.46% p<0.001 sig, vs SPY +0.12% p0.374 NS, vs QQQ -0.19% p0.147 NS — outperforms architectural baselines but not sig vs QQQ absolute (but risk-adj superior)
- Sensitivity (Table 11): Pos size 5-20% ±0.8%, Premium thresh 1.5-4% ±1.2%, Rolling 3-8% OTM ±0.6%, Temp 0.05-0.3 ±0.4% robust
- Ablation (Table 12): LLM-gen 15.3% 1.08 -8.2%, Random best of 1000 9.2% 0.67 -18.7%, Fixed template 11.5% 0.82 (expert-designed similar level)
- Consistency (Table 13): 25 scenarios ×20 variations=500 nets, Jaccard similarity 0.78 mean, perf variance 0.8% stdev annual returns — moderate structural variation but stable perf
- Structural variation impact (Table 14): edges vol→strike +2.1%, market regime→assignment prob +1.8% correlate with outperf
- Reliability (Table 15): 68% variations 0.8-1.0 similarity ±0.5% perf impact, only 4% low 0.6-0.7 ±2.3% impact — acceptable prod reliability

## Case Studies
- COVID Mar 2020: Feb network prioritized technical 3% assign 20% size. Mar restructured emphasize vol → assign prob 15% size 10% → DD limited 18.3% recovered July 2020
- Bull 2021: size 25% aggressive 8% OTM higher contract counts portfolio growth → $332k premium 45.9% return

## Implementation Details
- LLM: gpt-4-0613 temp 0.1 max_tokens 2000 top_p 0.9 freq/pres penalty 0, alternatives Claude-3.5-Sonnet, gpt-3.5-turbo fallback
- Prompts: SYSTEM_PROMPT demands valid JSON {nodes,edges,reasoning} DAG no cycles causal not correlation include market+psych vars. Context prompt includes ticker, price, vol, trend, VIX, regime, FOMO, confidence, stress, tilt risk
- Parser: json regex {.*} DOTALL + fallback structured text parsing node_patterns (nodes|variables|factors) edge_patterns (->, influences, affects, causes) + validate_structure check fields nodes edges each len2 in nodes no cycles DFS
- Pipeline 8 stages: construct_prompt → llm generate_bn → parse → validate construct DAG → build pgmpy BN → populate CPTs from data → update BN → return
- Error handling: retry MAX_RETRIES LLM, fallback template generation, fallback predefined BN per regime
- Psychological state: FOMO, confidence, stress, tilt risk included — ties to Reddit trader case we tracked (FOMO over AMD etc)

## Limitations (paper honest)
- LTD of BN construction validation: no ablation vs expert/random initially, no consistency analysis, no stability analysis (they add in appendix now)
- Narrow scope: single strategy wheel only, equity options only, not bonds/commod/fx/crypto, single asset class, geographic limited US, need multi-strategy, multi-asset, EU/Asia/EM testing

## Conclusion Model-First Value
- Complete transparency vs black-box, consistency deterministic outputs same input same decision, adaptability without retraining LLM restructures for new regimes
- Each decision audit-ready 27 factors traceable human-interpretable e.g. "High vol + bearish regime increased assign risk so conservative strike"
- Demonstrates coupling LLMs + formal probabilistic reasoning = best both: contextual intelligence + math rigor → tool domain experts can understand/verify/trust

## Application to Our Wheel
- Our scoring already deterministic, but we lack context analyzer for regime. Could add LLM regime detection (VIX, trend, news) to dynamically adjust DELTA/EXP/YIELD
- Rolling engine currently missing — paper proves 371% roll rate key to 0% assign and -8.2% DD vs -60%. Implement core/roller.py
- Logging 27 factors: we log delta/OI/yield/score, need add vol level, OTM%, premium rate, risk, position size, market context, rationale → expand strategy_logger.py
- Feedback loop: after each trade outcome, adjust filter params — we did manual feedback today (YIELD_MAX 0.06→0.50 after 0 pass). Automate
- CPT based on Optionable history: use our own trades (currently 5) to build prob tables eventually after 100+ trades, like paper uses 8919
- Psychological safety: track FOMO (like NVDA trader who branched to AMD) to avoid overconcentration — paper includes FOMO level explicitly
