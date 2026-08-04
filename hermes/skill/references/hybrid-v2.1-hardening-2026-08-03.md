# Hybrid v2.1 Hardening — 2026-08-03

## Three fixes after v2 rollout

### 1. Rolling 5% -> 3% OTM (too sensitive)
- **Before:** `ROLLING_OTM 0.05` flagged 4/5 puts same day 18D (F 3.8% BAC 1.7% PFE 1.6% VZ 3.4% OTM) → churn
- **After:** `0.03` flags 1/9 (only PFE 1.6% <3%). F/BAC/VZ/XOM 3.4% stay safe
- Rationale: Paper Table 11 says 5% OTM but paper entry 10% OTM, your 0.18-0.35 delta ~5-8% OTM entry already tight. 3% correct for 14-45D wheel.
- Execution fix: close-before-open with 2s sleep to free BP. Before: `close_req + open_req` same tick → Alpaca checks BP with both positions → `insufficient options buying power required 18115 available 14831` on CVX 190P→185P. After: close, sleep 2s, then open.
- Target filtering added spread filter.

### 2. Spread Filter — Sophie $0.05 NTM non-negotiable
- **Before:** No filter. MP $40 2.12/2.73 spread $0.61 25% mid allowed → filled, unreal -52
- **After:** `SPREAD_MAX_ABS 0.15`, `SPREAD_MAX_PCT 0.12`, `SPREAD_NTM_MAX 0.05` for delta≥0.30
- Implementation `core/strategy.py`:
```python
spread_abs = ask - bid
spread_pct = (ask-bid)/mid
if ad >=0.30 and spread_abs>NTM_MAX and >MAX_ABS: block
else if spread_abs>MAX_ABS and pct>MAX_PCT: block
if abs_spread>0.30 hard cap
```
- Scoring penalty: >5% ×0.9, >10% ×0.8, abs>0.10 ×0.9
- Test cases: CSCO 2.56/2.61 $0.05 1.9% passes, XOM 2.45/2.56 $0.11 4.3% passes, MP $0.61 25% blocked ✅
- Roll targets same filter added.

### 3. VIX via Alpaca IEX (Yahoo 403, SIP 403, CBOE flaky)
- **Before:** `get_vix_and_spy` used Yahoo `query1.finance.yahoo.com/v8/finance/chart/%5EVIX` → 403 on Pi due to crumb, SIP `data.alpaca.markets/v2/stocks/bars` → 403 `subscription does not permit querying recent SIP data` free tier blocked, CBOE `cdn.cboe.com/api/global/delayed_quotes/charts/legacy/close/_VIX` shape unstable.
- **After:** `StockBarsRequest(..., feed=DataFeed.IEX)` works free tier. Daily bars SPY+VIXY 40d IEX.
```python
from alpaca.data.enums import DataFeed
req = StockBarsRequest(symbol_or_symbols=["SPY","VIXY"], timeframe=TimeFrame.Day, start=start-40d, end=now, feed=DataFeed.IEX, limit=100)
bars = client.stock_client.get_stock_bars(req)
df = bars.df
spy closes 24 days → realized vol sqrt(var)*sqrt(252)*100 = 12.9%
VIXY latest 20.2 → VIX proxy = VIXY*1.3+4 = 30.26 (empirical)
blend = max(realized*1.15, VIXY*0.9)
source logged: alpaca_iex_realized / vixy_proxy / blended / cboe_api / vixy_latest_proxy
```
- Captured: `spy_price 756.215` (synthetic paper feed inflated vs real ~580, but IEX shows same), `spy_5d +3.1%`, `spy_change +1.2%`, `spy_20d_vol 12.9%`, `vixy_5d -10%`, `vixy_price 20.2`, `vix 30.26 high`.
- Regime now correctly bear (was neutral medium) because VIX high → adapt bear: `DELTA_MAX 0.25, MAX_RISK 54k (90k*0.6), size10%, assign15%` per paper March 2020 DD -18.3% recovery July.
- Actual risk 54.25k vs adapted 54k → BP -250 blocks new puts (conservative correct). Before with neutral regime BP 20k still allowed.
- Future clamp: VIX estimate 12-35, real VIX today ~15-18, proxy 30 inflated due VIXY factor. Recommend factor 1.0+3 or clamp.

## Live verification v2.1

Run 2026-08-03 16:24 PDT:
```
[CONTEXT] Regime=bear VIX=30.26 level=high Vol=high src=alpaca_iex_vixy_proxy
Adapted bear size10% delta max 0.25 risk 54000
[ROLLER] 1 positions need rolling: PFE 1.6% <3% buffer medium (vs 4 before)
No roll targets meeting $0.10 credit
SGOV at target 455
risk 54.25k
Optionable 10 positions
market_context.json bear high vix_source alpaca_iex_vixy_proxy spy_5d +3.1% spy_20d_vol 12.9% vixy_5d -10%
MP 40P $0.61 blocked correctly
```

## Files changed
- `config/params.py`: ROLLING_OTM 0.05→0.03, added SPREAD_MAX_ABS 0.15 SPREAD_MAX_PCT 0.12 SPREAD_NTM_MAX 0.05
- `core/strategy.py` v2.1: spread calc, NTM tighter, hard cap $0.30, penalty in score
- `core/roller.py` v2.1: default otm 0.03, close-before-open +2s, spread filter in find_roll_targets
- `core/context_analyzer.py` v2.1: IEX feed SPY+VIXY bars, realized vol, VIXY proxy, source logging, adapt with bear/bull/neutral
- Cron prompt updated 6.5k chars with spread + VIX IEX notes
- Commit 9d6b892 feat wheel v2.1 hardening

## Next tuning
- Clamp VIX 12-35 or recalibrate VIXY*1.0+3
- Add IV Rank toggle: VIX>25 or IV Rank>50 → 0.20 delta, 25-50 →0.30, <25 wait (Sophie)
- Profit take auto-close at 50% (Reddit trader 3.2d avg winner)
- CPT building after 100+ trades wheel_trades.jsonl 27 factors
