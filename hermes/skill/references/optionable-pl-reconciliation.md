# Optionable P/L Reconciliation vs Alpaca Real P/L (2026-08-04)

## Problem
Optionable UI shows OPTIONS P/L $568 = sum(entryPrice*100) for status=Closed when closePrice=0. It ignores buy-to-close cost, so rolls appear as full profit.

Example:
- DB trades 9-23: 15 total, 3 Closed (BAC $61 $66, CVX $190 $312, INTC $77.5 $190) = $568
- Alpaca fills: BAC $61 sold 0.66 bought 0.69 = -$3 rolled to $60P 1.05 open, CVX 3.10→3.35 = -$25 closed, INTC 1.90→1.10 = +$80 closed
- Realized = +$52, Unrealized = $2,036 (12 open CSPs risk $81.75k)

## Verification
```bash
curl -s http://localhost:8096/api/trades | jq
# OPTIONS P/L in UI

python3 -c "
import sqlite3
con=sqlite3.connect('/home/smitpatel316/optionable-data/optionable.db')
cur=con.cursor()
cur.execute('SELECT id,ticker,strike/100.0,entryPrice/100.0,closePrice/100.0,status FROM trades ORDER BY id')
for r in cur.fetchall(): print(r)
"

# Alpaca real fills
tool_search mcp__alpaca__get_account_activities
# FILL sell_short premiums vs buy closes

# Browser http://localhost:8096/ shows ticker breakdown CVX $312 INTC $190 BAC $66
```

## Fix Needed
- optionable_sync.py sync_closed_trades() must write actual closePrice from Alpaca buy_to_close fills, not leave 0
- push_trade_to_optionable should update closed trades with closePrice when rolling
- Until fixed, always cross-check Optionable vs mcp__alpaca__get_account_activities

## How to Explain $500 Claim
1. Show Optionable dashboard: TOTAL P/L $568 3 closed trades
2. Breakdown ticker P/L table
3. Show DB sum matches UI
4. Then show Alpaca true economics: premium collected $20.88*100=$2088, buy costs $514, net realized +$52, rest unrealized at risk
5. Explain gap is accounting bug, not real profit

Related: trades table schema entryPrice INTEGER cents, closePrice INTEGER, status enum, accountId, commission.
