# Agentic Run 2026-08-03 15:35 ET Final (12:07 PDT) — Fully Utilized Conservative HOLD

**Clock:** is_open:true next_close 16:00 ET timestamp 15:07:58 ET
**Account:** equity $99,826.17 P/L -$173.83 (-0.17%) cash $91,229.79 buying_power $36,163 regt $13,903 options_bp $6,951 PA3WFOAHE2C6 level3 mult 4x ACTIVE

**Positions 14:** 13 CSPs risk $89,500 + SGOV 104 @100.42
- BAC 60P Sep18 46D 1.05→1.02 +2.86% +$3 und 62.29 OTM 3.82% delta -0.2959 hold >3%
- CSCO 108P Aug21 18D 2.59→2.81 -8.49% -$22 und 116.135 OTM 7.53% delta -0.2691
- F 14P 0.24→0.29 -20.8% -$5 und 14.495 OTM 3.54% delta -0.3225
- INTC 77.5P 1.9→1.9 0% $0 und 90.57 OTM 16.86% delta -0.1792 safe deep OTM
- KO 85P 0.83→0.82 +1.20% +$1 und 86.95 OTM 2.29% delta -0.2987 **FLAGGED <3% defensive medium**
- MP 40P Sep11 39D 2.14→2.63 -22.9% -$49 und 44.045 OTM 10.11% delta -0.2965
- NEE 82.5P Sep18 46D 1.3→1.43 -10% -$13 und 86.02 OTM 4.27%
- PFE 24.5P 0.33→0.35 -6.06% -$2 und 25.035 OTM 2.18% **FLAGGED**
- SBUX 100P Sep18 46D 2.39→2.44 -2.09% -$5 und 104.28 OTM 4.28%
- T 22.5P 0.24→0.23 +4.17% +$1 und 23.575 OTM 4.78%
- VZ 46P 0.49→0.50 -2% -$1 und 47.49 OTM 3.24%
- WFC 85P 1.22→1.14 +6.56% +$8 best und 87.39 OTM 2.81% **FLAGGED**
- XOM 150P 2.4→2.9 -20.83% -$50 und 154.21 OTM 2.83% **FLAGGED**
- SGOV 104 long $10,444.72 @100.425

Risk: put_exp 89,500 long 0 total risk 89,500/90k 99.4% utilized, BP $500 remaining, options BP tight $6,951.

**Phase1 Context v2.2 Yahoo v8 VIX Real**
```
VIX 15.75 medium source yahoo_v8_vix SPY 758.375 +1.52% day SPY_5d +1.35% vol 16.2% vixy_5d -9.9% fear dropping
Regime neutral vol medium tech neutral BN balanced 30-45DTE 0.30 delta Sophie+paper size15% MAX_RISK 90k full
Adapted: DELTA_MAX 0.30 DELTA_MIN 0.18 EXP 14-45 MAX_RISK 90000 PCT 0.75 POSITION_SIZE 15% ROLLING_OTM 0.03
```
Yahoo v8 primary `query1.finance.yahoo.com/v8/finance/chart/%5EVIX` returns closes 15.75 real, IEX fallback `StockBarsRequest DataFeed.IEX` SPY 20d vol 16.2% = sqrt(var)*sqrt(252)*100. Sources logged yahoo_v8_vix.

**Phase2 Closer Option A 50%**
`evaluate_all_for_close` config profit 50% DTE>3, time 40%+$0.20 DTE7-21, DTE_min 3
- 0/13 should_close avg -7.4% vs -11% previous run (theta helping), best WFC +6.56% worst MP -22.9% XOM -20.8%
- Near miss >=25%: 0 positions
- Conservative HOLD correct per Reddit early close + Sophie 50% rule, 75%+ high urgency lock, max 3 profit-sorted buy_to_close wheel-close-*

**Phase3 Roller 3% OTM v2.2**
`evaluate_all_positions` config rolling_otm 0.03 dte_critical 3 delta_threshold 0.50 loss 1.0 profit 0.50 min_credit 0.10 spread 0.15/12%
- 4 flagged <3% medium defensive: KO 2.29% PFE 2.18% WFC 2.81% XOM 2.85% (underlying prices via `_parse_occ` + `get_stock_latest_trade([underlyings])` accurate, previously 0 before fix)
- Roll target search: `get_options_contracts([underlying], put)` = 158-218 contracts 14-60DTE each, snapshots batch 100 `get_option_snapshot` -> available filtered near ATM (strike <= current, DTE ext +7 to +35, bid>=0.20). Result **0 available** because Alpaca snapshot for deep OTM returns bid_price 0.0 ask 0.01 greeks None (e.g., KO260821P00045000 quote timestamp 2026-07-31 bid 0.0 size 0.0 ask 0.01 size 9.0). Contract builder filters bid<0.20 excludes, correct.
- `find_roll_targets` defensive lower strike first net_credit desc, requires net_credit >=0.10 (close cost KO $0.82 needs bid >=$0.92), spread abs<=0.15 pct<=12% hard $0.30. Targets found 0 -> **conservative HOLD Option A correct** (SPY +1.26% up day, marginal credits on 13:05 run were $0.11-$0.12 but now stale)
- No rolls executed, avoids 403 insufficient BP via close-before-open +2s fix.

**Pitfall — Stale Quote Filtering**
- Alpaca `OptionSnapshot` for deep OTM returns `latest_quote bid_price=0.0 bid_size=0.0 ask_price=0.01 size=9.0 conditions='A'` and `greeks=None`. Building `Contract` with bid>=0.20 filter correctly yields 0 available for some underlyings when all near ATM still have bid 0 due to market closed? Actually during market hours still many bid 0 for far OTM. This is not an error — it means no viable defensive roll meeting net credit + spread. Log as HOLD, not failure.
- Fix: Don't lower MIN_PREMIUM to chase — keep $0.20 floor, require spread filter, net_credit $0.10. If 0 targets, hold.

**Phase4 Wheel Sells**
- Symbol list 25, existing 14 incl SGOV, allowed 12: AAPL AMD PG JNJ CVX HON CAT DUK LIN DLR PLD SPY
- `filter_underlying(client, allowed, BP=500)` -> 0 because 100*price <=500 none. BP = MAX_RISK 90k - risk 89.5k = $500 < $2000 min Option A wait guard.
- Skip new puts correct. Will resume after closer profit takes free BP. Total raw contracts if attempted would be ~5000+.

**Phase5 SGOV Treasury Proxy**
- idle = 100k - 89500 = 10500, target_cash 10500, price $100.42 latest trade 100.42, qty 104, target floor(10500/100.42)=104 diff 0 at target.
- Open orders 0 via `get_orders filter OPEN`, guard duplicate OK.
- Log: `[SGOV] Alpaca SGOV 104x$100.42 target 104 (10500) diff 0 | put $89500 long $0 idle $10500 cash $91229 risk $89500`

**Phase6 Optionable Tracker Sync**
- `OPTIONABLE_URL=http://localhost:8096` env not credentials
- `sync_alpaca_equity_to_optionable` + `sync_sgov_to_optionable` + `sync_closed_trades` ok
- Health 200 v0.16.0 tradeCount 15, envelope `{success:true, data:[...], meta:{pagination}}`
- Trades 15: open 13 closed 2 (BAC 61P rolled → 60P Closed, CVX 190P Closed)
- Open premium sum $1,723 avg $1.32×100, previously reported $2,170 included closed
- Stocks: SGOV 104 costBasis 100.43 id 20
- Delta abs() fix live, idempotent DELETE before POST

**Phase7 Activities + Logging**
- `get_account_activities_by_type` DIV 0 INT 0 FEE 0 OPASN 0 OPEXP 0 clean
- `activities_sync.py` would POST to `/api/fund-transactions`
- Logs: `logs/cron.log` appended full 16-line entry, `market_context.json` 8 contexts last 15.75 neutral, `strategy_log.json` 236k, `wheel_trades.jsonl` 11 lines 27 factors CPT
- MCP tools used this run: get_clock, get_account_info, get_all_positions, get_watchlist_by_id, get_orders, get_account_activities_by_type x5, broker_client get_options_contracts + get_option_snapshot + get_stock_latest_trade for scoring

**Outcome**
Fully utilized 99.4% no trades, conservative HOLD Option A correct. Next trigger theta decay 5-8d to 25-50% profit, roller only if net_credit >=$0.10 + spread passes + ITM break, or Aug21 expiry freeing BP. EOD 3:35pm final check expected same hold unless market moves.

**Verification**
```bash
cd ~/options-wheel && ~/options-wheel/.venv/bin/python -c "from config.credentials import *; from core.broker_client import BrokerClient; cli=BrokerClient(ALPACA_API_KEY,ALPACA_SECRET_KEY,IS_PAPER); print(cli.get_stock_latest_trade(['KO','PFE','WFC','XOM']))"
curl -s http://localhost:8096/api/health | jq
curl -s http://localhost:8096/api/trades | jq '.data | length'
cat ~/options-wheel/logs/cron.log | tail -40
```
