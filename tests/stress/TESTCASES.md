# Wheel Engine Stress-Test Case Catalog

Offline-only. Every test runs against `FakeBrokerClient` (tests/stress/fakes.py) — no network,
no Alpaca/Alpha/Yahoo/Finnhub, no Optionable. Strategy parameters are NEVER modified by these tests;
param-level observations are report-only.

## R — Regression: this week's real production bugs
- R1 Liquidity filter must NOT silently drop symbols when liquidity_map is populated (bug fixed in 3c09d82: `safe.append(sym)` was missing). Symbols with trend_ok=True survive; only avg_5d < 300k with trend_ok=False are dropped.
- R2 sell_puts breaks the candidate loop when Alpaca rejects with "insufficient ... buying power" (50a0793) — remaining candidates are not attempted.
- R3 Candidates over Alpaca options BP are skipped with a log line, cheaper candidates still tried (75795d5).
- R4 SGOV-funds-CSP (5115019):
  - R4a sale fills & BP refreshed -> put proceeds
  - R4b sale fills but BP still short -> candidate skipped, no put sold
  - R4c SGOV sale throws -> skip, no put sold
  - R4d no SGOV held -> skip, no put sold
  - R4e deficit math rounds up (ceil) and is capped at SGOV holdings
  - R4f risk-cap BP < need -> never even attempt the SGOV sale
  - R4g market-closed / fill pending -> skip without selling the put
- R5 Optionable sync failure must not abort the loop (warn + continue).
- R6 sell_calls with <100 shares logs and returns — must not raise (the raise killed whole runs historically).

## C — Capital accounting & margin safety
- C1 New CSP never submitted when options_buying_power < need, even with huge margin-inflated stock BP (enforce_options_bp proves the engine pre-checks).
- C2 buying_power (risk cap) is decremented per sale and refunded on failure.
- C3 Zero/negative risk BP -> sell_puts returns immediately, no orders.
- C4 SGOV sweep target math: target = min(total_liquid - $500, stockBP - $1000 + sgov_mv); buys the diff at market when under, sells at market when over, no-op at target.
- C5 Sweep never double-buys while an open SGOV buy order exists.
- C6 Sweep treats SGOV as treasury: excluded from wheel risk (calculate_exposures).
- C7 MAX_RISK gate in run_strategy: risk >= MAX_RISK -> no new CSPs (source-level + exposure math).

## S — sell_puts loop behavior
- S1 Candidates are attempted in score order (best first).
- S2 Multi-candidate partial fills: fills until BP exhausted.
- S3 All candidates over options BP -> zero orders, clean logs.
- S4 Zero candidates after filtering -> "No put options" path, no crash.
- S5 Score lookup survives put_options.index(p) mismatch (no crash, score 0).
- S6 Empty get_stock_latest_trade response -> whole run aborts gracefully (no CSPs).
- S7 Missing symbols in price response are tolerated.

## X — Execution (place_limit_or_market_sell)
- X1 Limit fills at mid -> records fill + improvement.
- X2 Limit unfilled -> cancel + market fallback.
- X3 Limit throws -> market fallback.
- X4 Market also fails -> exception propagates to caller (sell_puts refunds BP).
- X5 calc_mid_price: bid/ask zero, None, one-sided quotes.
- X6 Limit price never below bid (limit_price >= bid guard).

## CL — Closer (50% profit taker)
- CL1 Exactly 50.0% profit -> closes (>= boundary).
- CL2 49.9% profit -> no close.
- CL3 DTE <= 3 blocks close unless profit >= 75%.
- CL4 75%+ profit closes regardless of DTE (incl. DTE 0-3).
- CL5 Time-efficient path: 40-50% profit, DTE 7-21, >= $0.20 abs -> close.
- CL6 Time path boundary: DTE 6 -> no; DTE 22 -> no.
- CL7 current_price $0 (stale feed) -> profit computed vs 0 (would be 100%) — document behavior (phantom $0 protection lives in optionable_sync, not closer math).
- CL8 Multiple positions -> multiple decisions.
- CL9 close_position failure returns False, does not raise.
- CL10 Fees: paper IS_PAPER=True -> $0 commission in net P/L.

## RO — Roller
- RO1 OTM 2.9% (< 3% buffer) -> defensive roll, medium urgency.
- RO2 OTM exactly 3.0% -> no roll from buffer rule.
- RO3 ITM (negative OTM) -> high urgency.
- RO4 DTE <= 3 near ITM -> critical.
- RO5 DTE <= 1 and OTM < 1% -> critical + debit allowance -$0.20 in find_roll_targets.
- RO6 Delta > 0.50 -> roll; > 0.60 -> high urgency.
- RO7 Loss > 100% -> defensive roll.
- RO8 No valid roll targets -> empty list, no crash (the Ford saga).
- RO9 find_roll_targets: strike above current rejected for defensive puts; DTE extension window enforced (>= +7d, <= +21+30d).
- RO10 Debit roll boundary: net credit -0.19 allowed only when critical DTE<=1; -0.21 rejected; non-critical always rejects debit.
- RO11 Roll execution: close-before-open ordering (close submitted before open).
- RO12 Underlying price 0 (missing trade) -> itm math doesn't crash, roll decision still sane.

## SC — sell_calls
- SC1 <100 shares -> log + return, no orders (regression: used to raise and kill the run).
- SC2 Dividend-risk block -> no orders.
- SC3 Normal path sells best call.
- SC4 No viable calls -> clean return.

## F — Filters
- F1 filter_options reject-reason tally: all-rejected chain tallies sum to input size.
- F2 Delta band: below DELTA_MIN / above DELTA_MAX rejected; vol_map delta_max override honored.
- F3 Premium floor MIN_PREMIUM.
- F4 No ask / no delta / low OI / zero strike each rejected with the right bucket.
- F5 Yield bounds YIELD_MIN/YIELD_MAX.
- F6 Earnings: blocks within 3 days, blocks within DTE 21, allows after earnings date (days_until < 0).
- F7 Fundamentals blocked symbol skipped; missing symbol passes.
- F8 filter_underlying BP drop: 100*price > limit dropped.
- F9 Vol_map high IV note doesn't remove symbols.
- F10 Dividend map only applies to calls (is_call=False ignores it).

## SG — SGOV sweep (sync_sgov_real)
- SG1 Under target -> market BUY of the exact diff.
- SG2 Over target -> market SELL of the exact diff (assignment path).
- SG3 At target -> no order.
- SG4 Open buy order exists -> no duplicate buy.
- SG5 SGOV price fallback when quote missing -> doesn't crash, uses position price.
- SG6 Both directions are market orders (Smit 2026-08-14 rule).

## P — IS_PAPER discipline
- P1 credentials.IS_PAPER is True in repo.
- P2 No code path constructs a client with paper=False outside tests/dev scripts (grep audit).
- P3 run_strategy refuses to run live unless explicitly configured (source audit of IS_PAPER usage).
