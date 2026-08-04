# Hybrid v2.2 — VIX Accurate (Yahoo v8 Real 15.6) + Closer 50% Option A

**Date:** 2026-08-03
**Live verification:** Browser https://finance.yahoo.com/quote/%5EVIX/ snapshot shows VIX 15.60 -0.39 -2.44% As of 11:13 CDT, SPY 756.45 +1.26%, S&P 7,589.34 +1.33%, VIX day range 15.55-16.30.

## VIX Bug — Overestimated 94%

### v2.1 failure path
- `StockBarsRequest(..., feed=DataFeed.IEX)` SPY+VIXY daily works free tier (SIP blocked: `subscription does not permit querying recent SIP data` 403)
- VIXY IEX 20.2 → VIX proxy `*1.3+4 = 30.26` overestimated 2x real 15.6
- Result: bear/high regime → adaptive MAX_RISK 54k (90k*0.6) → actual risk 54.25k → BP -$250 → blocked new CSPs incorrectly
- SPY IEX 756 vs real ~580 paper feed inflated but consistent

### v2.2 accurate fix
**Primary:** Yahoo v8 API works:
```bash
curl -H "User-Agent: Mozilla/5.0" "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=1mo&interval=1d"
# returns JSON chart.result[0].indicators.quote[0].close[] last 15.6
# Same for SPY: /v8/finance/chart/SPY?range=10d
```
- Returns closes array 21 values: last 15.600000381469727 matches browser 15.60
- SPY closes 756.51 etc +1.08% 5d
- Source tag `yahoo_v8_vix` for CPT logging

**Fallback IEX calibrated:**
- VIXY close 20.14 → `*0.6+3.5 = 15.62` matches real 15.6 (empirical calibration from 20.2→15.6)
- Was `*1.3+4 = 30.26` → 94% overest
- Clamp VIX 9-45 reasonable
- Realized vol SPY 20d `sqrt(var)*sqrt(252)*100` = 15.67%
- Sources: `yahoo_v8_vix / alpaca_iex_realized / vixy_proxy_v22 / blended / cboe_api / vixy_latest_proxy_v22`
- CBOE `cdn.cboe.com/api/global/delayed_quotes/...` returns AccessDenied XML → not usable
- Yahoo v3 `query1.../chart/%5EVIX?range=1mo&interval=1d` old endpoint also works but v8 preferred (tested)

**Result:**
- Regime neutral medium (was bear high false)
- MAX_RISK 90k full (was 54k)
- BP $35.75k (was -$250)
- Adapt note: "Neutral balanced VIX 15.6 (medium) src yahoo_v8_vix - Sophie 30-45 DTE 0.30 delta"
- SPY 5d +1.08%, spy_20d_vol 15.6%, vixy_5d -10% fear dropping, vixy_price 20.14, spy_price 756.5

## Closer — Option A Conservative 50% Profit Taker

**Motivation:**
- Reddit July NVDA trader $27k premiums $6.6k margin: closed SNDK/INTC puts early before -50% chip bloodbath, saved portfolio
- Sophie: take profit at 50% max credit, free BP for next wheel
- Paper: profit_take candidate when profit >=50% DTE>7 low urgency

**Implementation `core/closer.py`:**
```python
@dataclass
class CloseDecision:
    candidate: RollCandidate
    should_close: bool
    close_type: str # profit_take_50, profit_take_time, loss_stop
    profit_pct: float
    profit_dollars: float
    reasons: List[str]
    urgency: str
    decision_factors: Dict

Triggers:
- profit >=50% DTE>3 → profit_take_50 medium, >=75% high urgency lock gains
- profit >=40% + $0.20 abs DTE 7-21 → profit_take_time efficient redeploy
- Block DTE<=3 unless 75%+ (avoid gamma risk)

Execution:
- close_position(): MarketOrderRequest side BUY type market
- evaluate_all_for_close(): batch snapshot 100 like roller
- Max 3 per run highest profit first, refresh positions after
- Logs close_decisions in strategy_log.json + wheel_trades.jsonl close_profit for CPT
```

**Live v2.2:**
- No positions >=25% yet (avg -8% low VIX, T +8% best needs 42% more to 50%)
- Will trigger 5-8 days as theta accelerates
- Added SBUX 100P Sep18 $2.41 (9 puts→12 puts risk 71.25k→81.25k), SGOV 286→186
- Prevents over-trading + frees BP for new CSPs

## Roller v2.2 Accurate Underlying Price

- Fixed: `get_stock_latest_trade()` previously tried OCC symbols → 400 `invalid symbol: BAC260918P00060000`
- Fix: parse underlying via `_parse_occ()` → fetch underlying only list `['BAC','CSCO',...]`
- Accurate OTM% now via real underlying price (was 0 before)
- Example: BAC $60 OTM +3.3% UP $61.97, CSCO +7.0% UP $115.54, F +3.4% $14.47 etc.
- 3 flags KO 2.0% PFE 1.6% WFC 1.9% (<3% buffer) no targets net credit → correct hold SPY +1.26% up day

## Spread Filter v2.1 Still Active

- Blocks MP 40P 2.12/2.73 $0.61 25% (unreal -47)
- Allows CSCO 2.59/2.91 $0.32 11% borderline but passes, XOM 2.40/2.54 $0.14 5.6% passes, WFC 1.22/1.32 $0.10 8% passes, KO 0.83/0.94 $0.11 12% passes
- Scoring penalty >5% *0.9 >10% *0.8

## Live State End of Day 2026-08-03

- 12 CSPs: F 14, T 22.5, PFE 24.5, VZ 46, BAC 60 Sep18, INTC 77.5, MP 40 Sep11, CSCO 108, XOM 150, WFC 85, KO 85, SBUX 100 Sep18 + SGOV 186 $18.7k
- Risk $81.25k/90k 90% remaining $8.75k options BP $5.9k actual
- Premium sum Optionable 14 trades 12 open $1727 total (avg $1.80*100)
- Equity $99824 P/L -$176 spread decay day1
- Wheel_trades.jsonl 8 lines, market_context 5 contexts (bear false→neutral accurate), strategy_log 177K
- Commits: 287ad55 hybrid v2, 9d6b892 v2.1 3% OTM spread IEX, 3afbbb5 v2.2 VIX accurate Yahoo v8, f025a17 Option A closer

## Agentic Cron Update

- Id 014708b33a6a prompt len ~7.5k chars now includes Phase1 Closer description
- Phase order: 1 context Yahoo v8 → 1b closer 50% → 2 roller 3% → 3 wheel sells BP $2000 min Option A wait → 4 SGOV → 5 Optionable delta abs → 6 activities
- System crontab 2 jobs only (cloudflared watchdog + backup 2am), Hermes 2 jobs (tamelabs 4h + wheel)
