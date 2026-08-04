# Quantitative Wheel Framework — Sophie AI Finance

Source: https://www.sophie-ai-finance.com/articles/options-wheel-trading-plan-quantitative-approach (2025-10-11)

## Core Principle
- Wheel = stock acquisition strategy, not speculative trading. Put-call parity C+X=P+S: CC mathematically equivalent to CSP same strike/exp.
- Performs best in neutral/sideways/mildly bullish. Underperforms buy-hold in strong bull, loses in strong bear but premium cushions.
- 80-90% options expire worthless. 16-30 delta ≈ 70-84% win probability.

## Underlier Selection Protocol (multi-stage filter)
- Fundamental: P/E <25 or below industry avg, Debt/Equity <0.7, 5-yr revenue >5%, div yield >1.5%
- Qualitative: Willing to own long-term? Yes, Strong moat
- Market-Based: Stock ADV >1M shares, Options OI >5,000 near-month, Bid-Ask <$0.05 NTM, high liquidity non-negotiable
- Portfolio: Position size <10% portfolio
- Final decision Qualify/Disqualify table

## Option Writing Protocol — DTE/Delta by IV Rank
- Sweet spot 30-45 DTE captures accelerated theta, avoids high gamma of weeklies.
- 0.20 delta ≈ 80% OTE, balanced; 0.30 delta balanced wheel; 0.40 aggressive acquisition.
- Matrix:
  - Conservative (avoid assignment): IV Low <25 wait/low premium, Med 25-50 45DTE 0.20Δ, High >50 45DTE 0.20Δ
  - Balanced (willing to wheel): Low 30-45DTE 0.30Δ, Med **30-45DTE 0.30Δ** (their bold), High 30-45DTE 0.30Δ put / 0.25Δ call (more upside room)
  - Aggressive (max premium): Low/Med 30DTE 0.40Δ, High 30DTE 0.40Δ put / 0.35Δ call

## Triple Income
1. Put premiums while waiting entry
2. Call premiums while waiting exit
3. Dividends during ownership
- Cost basis reduction: $50 strike - $2 premium = $48 effective. Subsequent calls further reduce = synthetic dividend.

## Greeks
- Delta: $1 move → Δ change in premium. For short puts 0.30 = +$0.30 if stock -1.
- Theta: primary profit source as writer, decays daily
- Vega: profit when vol decreases (vega crush)
- Gamma: rate of delta change, high = risk profile changes fast

## Scenario Analysis
- CSP Example: Stock $100, Sell $95P @ $2, cash $9500, breakeven $93. >$95 expires worthless +$200 max, $93 breakeven, $90 = -$300 paper loss but own 100 @ $95 effective $93.
- CC Example: Own 100 @ $48, Sell $50C @ $1.5, breakeven $46.5 max gain $350 capped above $50.

## Risk Management
- Bag-holding risk: biggest = assigned stock keeps declining. Mitigated by quality underlying you believe in.
- Opportunity cost: CC caps upside, trade-off for income.
- Rolling (Art):
  - Defensive: Lower strike, extend time, when position against you, reduce assignment prob, always net credit
  - Offensive: Higher strike, extend time, when profitable, maximize income & capital efficiency
- Position sizing: Never >5-10% per wheel position. Wheel = component of broader portfolio, not complete solution.

## Comparison Matrix
- Wheel: goal acquire discount + income, capital very high cash-secured, risk substantial (to zero less premium), profit capped by CC strike, ideal neutral-mild bull, active, advantage multi-stage income + basis reduction, disadvantage capped upside + bag-hold
- Buy-hold: long appreciation, high capital, substantial risk, unlimited profit, bull ideal, passive
- Dividend: passive income, high capital, substantial risk, unlimited + divs, any market, passive
- Credit spread: premium no own, low capital (spread width), defined limited risk, limited profit, neutral-directional, active, high capital efficiency

## Application to Our Pi Wheel
- Our fixed params DELTA 0.18-0.35 YIELD 0.008-0.50 EXP 14-60 OI 100 allow None MIN_PREMIUM 0.20 align with Balanced 30-45DTE 0.30Δ
- Gap before fix: OI >500 + YIELD_MAX 0.06 blocked real market yields 10-40% → fixed to allow None + 0.50
- Missing vs article: bid-ask filter should be added (from snapshot latest_quote), rolling engine (defensive/offensive) not yet implemented, IV rank adaptation not yet
- Scoring formula (1-|Δ|)*(250/(DTE+5))*(bid/strike) matches their risk-adjusted return concept; add liq boost 1.1 if OI>500 as we did
- Position size <10%: our MAX_RISK 75k/100k = 75% pool, but per-name 1.4-6.1% = compliant
