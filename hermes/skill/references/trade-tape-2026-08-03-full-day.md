# Options Wheel Trade Tape — Full Day Aug 3 2026 — 5 CSPs → 12 CSPs + Rolls

**Account start:** $100k paper PA3W, SGOV 496 @100.43 seed 03:41 ET FILLED + duplicate CANCELED (open-order guard)

**Root cause blocker:** 5132 contracts scanned 0 passed — YIELD_MAX 0.06 blocked 10-40% real yields + OI None blocked 2262/5132. Fixed YIELD_MAX 0.06→0.50, DELTA 0.30→0.35, EXP 45→60, OI 500→100 allow None, MIN_PREMIUM 0.20.

## Timeline ET (PDT -3h)

### 11:43-11:44 PDT Batch 1 — 5 CSPs after fix
- F260821P00014000 $14 18D Δ-0.296 OI 6787 $0.24 FILLED risk $1,400 wheel-F-14000-20260803-1
- T260821P00022500 $22.5 18D Δ-0.265 OI 725 $0.24 FILLED $2,250
- PFE260821P00024500 $24.5 18D Δ-0.346 OI 10488 $0.33 FILLED $2,450
- VZ260821P00046000 $46 18D Δ-0.292 OI 526 $0.49 FILLED $4,600
- BAC260821P00061000 $61 18D Δ-0.338 OI 1513 $0.66 FILLED $6,100
Total risk $16.8k premium $196 SGOV 497→828 +331 @100.43 FILLED Optionable tradeCount 5 health v0.16.0

### 12:18 PDT Batch2 Hybrid v2 first roller + context
- Context neutral VIX None medium balanced 30-45D 0.30Δ
- Roller 4 need rolling OTM<5%: BAC 1.7% F 3.7% PFE1.6% VZ3.4% → too sensitive 5% threshold
- BAC 61P BUY 1 @0.69 FILLED (close leg) + BAC 60P Sep18 SELL 1 @1.05 FILLED net credit +$0.38 defensive $61→$60 DTE 18→46
- INTC 77.5P 18D $1.90 FILLED Δ-0.1821 OI 1741 spread 5.2% score 0.237
- MP 40P Sep11 $2.14 FILLED Δ-0.3073 OI None spread 25.2% wide (pre-filter) score 0.209 risk $4k → later -22% unreal -$47
- CSCO 108P 18D $2.59 FILLED Δ-0.2777 OI 461 spread 16.2% wide score 0.186
- XOM 150P 18D $2.40 FILLED Δ-0.3199 OI 2539 spread 4.4% tight score 0.133
- SGOV SELL 373 @100.42 FILLED 828→455 risk $54.25k

### 12:19 PDT CVX + v2.1 test
- CVX 190P 18D $3.10 FILLED Δ-0.3403 spread 6.5% score 0.130 risk $73.25k SGOV 455→266 SELL 189 @100.42

### 16:23 PDT Roller BP bug
- CVX 190P BUY 1 @3.35 FILLED close leg for roll to 185P Sep18 net $0.26 but SELL opening failed 403 insufficient options buying power required 18115 available 14831 → close-before-open not freed. Fixed v2.1 +2s sleep.
- SGOV BUY 189 @100.43 FILLED rebalance after failed roll

### 16:29 PDT v2.2 VIX accurate Yahoo v8 15.6 real (was 30.26 overest)
- VIX 20.2*1.3+4=30.26 overest 94% → bear high adaptive MAX_RISK 54k BP -250 blocked. Fix Yahoo v8 ^VIX 15.6 real neutral medium → MAX_RISK 90k full BP $35.75k
- WFC 85P 18D $1.22 FILLED Δ-0.3452 OI 9307 spread 4.7% score 0.114
- KO 85P 18D $0.83 FILLED Δ-0.31 OI 3843 spread 1.2% tight score 0.083
- SGOV SELL 169 @100.42 455→286 risk $71.25k

### 16:36 PDT v2.2 + closer Option A
- Context neutral VIX 15.6 medium source yahoo_v8_vix SPY5d +1.08% vol 15.6% vixy_5d -10%
- Roller 3 <3% KO 2.0% PFE1.6% WFC1.9% no targets net credit correct hold SPY +1.26% up day
- Closer 0 ≥25% avg -8% T +8% best needs 42% to 50% hold Option A
- SBUX 100P Sep18 $2.41 FILLED Δ-0.3182 OI 2798 spread 2.9% score 0.089 risk $81.25k
- SGOV SELL 100 @100.42 286→186 BP $8.75k

### 16:42 PDT Final today NEE
- NEE 82.5P Sep18 $1.30 FILLED Δ-0.2724 OI 3783 spread 5.1% score 0.057 risk $89.5k
- SGOV SELL 82 @100.42 186→104 idle target $10.5k

## Final positions 14 total 13 puts + SGOV 104
- Risk $89.5k/90k 99.4% deployed, BP $500, options BP $6,952 tight
- Positions: BAC Sep60, CSCO 108, F 14, INTC 77.5, KO 85, MP 40 Sep11, NEE 82.5 Sep18, PFE 24.5, SBUX 100 Sep18, T 22.5, VZ 46, WFC 85, XOM 150 + SGOV 104
- uPL -$169 options spread decay day1, T +8% WFC +13% early winners, MP -22% worst wide spread
- Premium gross ~$20.5, net ~$16.5 after closes, Optionable $1,727 sum entryPrice*100 (14 trades 12 open 2 closed BAC61→60 roll + CVX190)

## Lessons for skill
- Close-before-open +2s delay critical for Alpaca options BP check (403 insufficient buying power)
- Spread filter v2.1 saves -$50+ per wide contract (MP -$47)
- VIX overest 94% flipped regime bear→neutral, MAX_RISK 54k→90k, BP -250→+35k — real VIX feed Yahoo v8 primary essential
- Option A BP min $2000 check prevents over-trading when 99% deployed
- Wheel tape log: order created_at, filled_avg_price, client_order_id pattern wheel-{ticker}-{strike}-{date}
