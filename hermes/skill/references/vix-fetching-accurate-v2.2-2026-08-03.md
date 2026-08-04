# VIX Fetching — Accurate Source v2.2 vs Failures

**Real VIX 2026-08-03:** 15.60 -2.44% low, SPY 756.45 +1.26%, S&P 7,589 +1.33%, day range 15.55-16.30, 52w 13.38-35.30 — browser snapshot https://finance.yahoo.com/quote/%5EVIX/ 11:13 CDT verified.

## Endpoint matrix tested on Pi budupi

| Endpoint | Auth | Result | Notes |
|---|---|---|---|
| `https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=1mo&interval=1d` UA Mozilla/5.0 | No crumb | **200 JSON** closes last 15.6 real | PRIMARY v2.2 source yahoo_v8_vix, returns chart.result[0].indicators.quote[0].close[] 21 values, also SPY same endpoint SPY 5d +1.08% |
| `https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=10d&interval=1d` | No | 200 JSON closes SPY 756.51 +1.08% | SPY momentum for regime, realized vol calc sqrt(var)*sqrt(252)*100 |
| `https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=1mo&interval=1d` old query1 without v8? Actually same as above | — | 200 (v8) vs 403 for v7 with crumb? v8 works | v8 is key, no crumb required with UA header |
| `https://cdn.cboe.com/api/global/delayed_quotes/charts/legacy/close/_VIX?limit=30` | No | **403 XML AccessDenied** `<Code>AccessDenied</Code>` | Not usable on Pi, requires residential proxy or CBOE API key |
| `https://cdn.cboe.com/api/global/delayed_quotes/quotes/list?symbols=_VIX` | No | 403 XML AccessDenied | Same block |
| `https://data.alpaca.markets/v2/stocks/bars?symbols=SPY,VIXY timeframe 1Day SIP` | Alpaca paper | **403 `subscription does not permit querying recent SIP data`** | Free tier blocked SIP, need IEX feed |
| `StockBarsRequest(..., feed=DataFeed.IEX)` SPY+VIXY daily | Alpaca IEX | **200 DF** SPY closes 24 values 738-756 NVDA, VIXY 20.14, realized vol SPY 20d 12.9-15.6% | Fallback, IEX works free tier |
| `get_stock_latest_trade(['SPY','VIXY'])` | Alpaca | 200 price SPY 756.215 IEX, VIXY 20.2 | Latest price for quick VIXY proxy |
| `get_stock_latest_trade(['BAC260918P...'])` OCC symbols | Alpaca | **400 invalid symbol** | Must parse underlying via _parse_occ() and fetch underlying only |

## Calibration

- VIXY proxy old: `VIXY*1.3+4 = 20.2*1.3+4 = 30.26` → 94% overest vs real 15.6
- VIXY proxy new calibrated empirical from real pair VIX 15.6 vs VIXY 20.14: `*0.6+3.5 = 15.62` matches real 15.6
- Derive: 20.14*0.6=12.08+3.5=15.58 close. For VIXY 20.2 → 15.62.
- Clamp VIX 9-45 reasonable.
- Realized vol SPY 20d: `rets = log(close[i]/close[i-1])`, `var = mean((r-mean)^2)`, `daily_vol = sqrt(var)`, `ann_vol = daily_vol*sqrt(252)*100` = 12.9-15.6%
- VIX proxy blended: `max(realized*1.15, VIXY*0.6+3.5*0.9)` + prefer Yahoo if available. Yahoo primary, IEX fallback.

## Sources tagging for Bayesian CPT

- `yahoo_v8_vix` — primary accurate
- `alpaca_iex_realized` — SPY 20d realized vol based
- `alpaca_iex_vixy_proxy_v22` — VIXY*0.6+3.5 calibrated
- `alpaca_iex_blended` — max of vol and VIXY proxy
- `vixy_latest_proxy_v22` — latest trade fallback calibrated
- `cboe_api` — legacy would be but blocked
- `alpaca_iex_vixy_proxy` old v2.1 overest

Logging in `market_context.json`: `vix_source`, `spy_5d`, `spy_20d_vol`, `vixy_5d`, `vixy_price`, `spy_price`

## Implementation in context_analyzer.py v2.2

```python
def get_vix_and_spy(client=None):
    result = {"vix":None, ...}
    # 1. Yahoo v8 primary
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=1mo&interval=1d", headers={"User-Agent":"Mozilla/5.0"}, timeout=6)
    # parse closes[-1] -> vix
    # SPY same endpoint for momentum
    # 2. Alpaca IEX fallback for VIXY and SPY bars if needed
    # 3. Clamp 9-45
```

**Impact:** VIX 30.26 high → bear regime adaptive MAX_RISK 54k BP -250 blocked. VIX 15.6 low real → neutral medium MAX_RISK 90k full BP 35k correct — 89.5k risk became possible, 12 puts placed instead of 9.

**Curl verification:**
```bash
curl -s -H "User-Agent: Mozilla/5.0" "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=1mo&interval=1d" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['chart']['result'][0]['indicators']['quote'][0]['close'][-1])"
# 15.6
```
