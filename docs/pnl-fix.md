# P/L Fix — $568 vs $52 Bug Root Cause

## Summary

Optionable UI showed OPTIONS P/L $568 but Alpaca real realized was +$52 on 2026-08-04. Gap due to closePrice=0 accounting.

## Symptoms

- **Optionable dashboard** 2026-08-04: 15 total trades, 3 Closed (BAC $61 $66, CVX $190 $312, INTC $77.5 $190) sum = $568 OPTIONS P/L
- **Browser ticker breakdown**: CVX $312 INTC $190 BAC $66
- **DB query**:
```sql
SELECT id,ticker,strike/100.0,entryPrice/100.0,closePrice/100.0,status FROM trades ORDER BY id;
-- shows closePrice 0 for closed trades
```
- **Alpaca fills** via `mcp__alpaca__get_account_activities_by_type` type FILL:
  - BAC $61 sold 0.66 bought 0.69 = -$3 rolled to $60P 1.05 open (not closed profit $66)
  - CVX 190P sold 3.10 bought 3.35 = -$25 closed (not $312)
  - INTC 77.5P sold 1.90 bought 1.10 = +$80 profit_take_time 42% FILLED (not $190)
  - Realized = +$52, Unrealized = $2,036 (12 open CSPs risk $81.75k premium $20.88*100=$2088 collected, buy costs $514)

## Root Cause — closePrice=0

In `core/optionable_sync.py`:

- `push_trade_to_optionable()` pushes sells with entryPrice dollars, status Open
- `sync_closed_trades(client)` logic 2026-08-03: compares Alpaca OCCs vs Optionable Open trades, if not in Alpaca positions => mark Closed via `PUT /api/trades/{id}` but **left closePrice at 0**
- Optionable schema `trades` table: entryPrice INTEGER cents, closePrice INTEGER, status enum Open/Closed/Expired/Assigned
- When status Closed and closePrice=0, UI P/L calculation = sum(entryPrice*100) for Closed ignoring buy cost → inflates profit
- Rolls appear as full profit: BAC60P $66 only counts sell side of original $61P, but real economics is -$3 + new $60P open $105

### Code Path

```python
# optionable_sync.py before fix
def sync_closed_trades(client):
    alpaca_positions = client.get_all_positions() # OCCs
    optionable_trades = GET /api/trades -> {data:[]}
    for trade in open_trades:
        if trade OCC not in alpaca_positions:
            if exp <= today: mark Expired closePrice=0
            elif stock exists: Assigned closePrice=0
            else: Closed closePrice=0  # BUG
            PUT /api/trades/{id} {status, closePrice:0}
```

Instead should fetch Alpaca fills buy_to_close for that OCC and write actual closePrice.

## Fix Implemented v2.5.4

### Fix 1: Sync writes real closePrice from Alpaca fills

```python
def sync_closed_trades(client):
    # new: fetch activities
    fills = client.get_account_activities_by_type('FILL') or MCP get_account_activities with type FILL
    # build dict OCC -> last buy_to_close price
    close_map = {}
    for fill in fills:
        if fill.side == 'buy' and 'P' or 'C' in symbol OCC:
            close_map[occ] = fill.price  # latest
    # when marking closed:
    actual_close = close_map.get(occ, 0)
    # if not found fallback 0 but log warning
    PUT /api/trades/{id} {status: Closed, closePrice: int(actual_close*100)}
```

Also handle commission: commission 0 paper via `_commission_for_trade()` else 0.65.

### Fix 2: Roll linking parentTradeId

When roller closes before open +2s, push new trade with parentTradeId linking to closed:

```python
push_trade_to_optionable(..., parentTradeId=closed_id, entryPrice new, closePrice for old update)
# In roller.py after buy_to_close success, update Optionable old trade closePrice via PUT before POST new
```

### Fix 3: Closer writes profit

```python
# closer.py close_position after buy_to_close market FILLED @1.1
sync_closed_trades(client) must update that specific OCC with closePrice 1.1
# then profit_dollars = (entry - 1.1)*100*qty = (1.9-1.1)*100 = $80 correct
```

### Fix 4: Optionable UI P/L Formula (if customizing Optionable fork)

If forking Optionable, change P/L calc from `sum(entryPrice)` when closePrice=0 to `(entryPrice-closePrice)*qty*100 - commission`:

```js
// in Optionable source components
pnl = trades.filter(t=>t.status!=='Open').reduce((s,t)=>s + (t.entryPrice - (t.closePrice||0))*t.quantity - t.commission,0)
```

But as we use `yomikoye/optionable:latest` image 642MB, we cannot change UI, must ensure closePrice populated via sync.

## Real P/L Calculation (Correct)

```
Premium collected = sum(sell_to_open) = $20.88 avg? Actually 12 open CSPs risk $81.75k premium $1723 sum? Wait live:
- Open 12 trades sum entryPrice $1723 open? Let's compute:

Real examples Aug 4:
- INTC 77.5P sold 1.90 buy 1.10 = +80 realized
- CVX 190P 3.10/3.35 = -25
- BAC 61P 0.66/0.69 = -3 rolled to 60P 1.05 open unrealized
Total realized = 80-25-3 = +52
Unrealized = sum(entry - current)*100 for 12 open = $2,036? Need snapshot greeks
Total = realized + unrealized marked

Formula:
realized_pnl = sum(sell_fills.price - buy_fills.price)*100 - fees (OPASN OPEXP $0.03 OCC + $0.02 CAT from activities_sync.py DIV0 INT0 FEE $0.05)
unrealized_pnl = sum(entryPrice - markPrice)*100 per open position via get_all_positions unrealized_p_l
equity = cash 55968 + sgov 45593 + positions market value? get_account_info equity $99k P/L -$235 etc
```

## Verification Commands

```bash
# DB direct
python3 -c "
import sqlite3
con=sqlite3.connect('/home/smitpatel316/optionable-data/optionable.db')
cur=con.cursor()
cur.execute('SELECT id,ticker,strike/100.0,entryPrice/100.0,closePrice/100.0,status FROM trades ORDER BY id')
for r in cur.fetchall(): print(r)
"
# also docker volume
docker run --rm -v optionable-data:/data alpine cat /data/optionable.db 2>&1 | head || sg docker -c "docker exec optionable ls -lh /data"

# Optionable API
curl -s http://localhost:8096/api/trades | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['data'][:3], indent=2))"

# Alpaca real fills
# via MCP from agent: mcp__alpaca__get_account_activities_by_type type FILL
# raw
curl -s -H "APCA-API-KEY-ID: $ALPACA_API_KEY" -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" https://paper-api.alpaca.markets/v2/account/activities/FILL | jq '.[:5]'

# Compare
python3 << 'PY'
# Load Alpaca fills vs Optionable closed
# Ideally script core/activities_sync.py already syncs FILL to fund
# For P/L: sum buy vs sell
PY
```

## How to Explain $500 Claim (from references/optionable-pl-reconciliation.md)

1. Show Optionable dashboard TOTAL P/L $568 3 closed trades
2. Breakdown ticker P/L table CVX $312 INTC $190 BAC $66
3. Show DB sum matches UI
4. Then show Alpaca true economics: premium collected $20.88*100=$2088, buy costs $514, net realized +$52, rest unrealized at risk $81.75k
5. Explain gap is accounting bug closePrice=0 not real profit — fix populates closePrice from Alpaca fills
6. Demonstrate after fix: curl trades shows closePrice non-zero, P/L recomputed matches Alpaca

## Prevention

- Always cross-check Optionable vs `mcp__alpaca__get_account_activities`
- Unit test sync_closed_trades with mock fills map OCC->closePrice
- Add to agentic prompt Phase 6 verification: after sync, query `SELECT COUNT(*) FROM trades WHERE status!='Open' AND closePrice=0` → warn if >0 indicates bug
- Log detailed_trades jsonl includes close_price for CPT

## Related

- trades table schema entryPrice INTEGER cents, closePrice INTEGER, status enum, accountId, commission, parentTradeId, openedDate, closedDate
- `core/optionable_sync.py` OCC regex `^([A-Z]+)(\d{6})([PC])(\d{8})$`
- activities_sync.py raw REST DIV/INT/FEE/OPASN/OPEXP sync to fund journal
- References: optionable-pl-reconciliation.md, full-stack-audit 2026-08-03, agentic-run-2026-08-04-1005ET
