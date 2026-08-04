# SGOV Real Alpaca Execution — Session 2026-08-03

## Problem
Previously `scripts/sync_sgov.py` only did Wheeler DB DELETE+INSERT (fake Treasury proxy). User required: "SGOV needs to be bought from Alpaca as well". Alpaca account showed 0 positions, $100k cash; Wheeler showed $49,957 fake.

## Solution: Real Market Orders via BrokerClient

### BrokerClient additions (core/broker_client.py)
```python
def market_buy(self, symbol, qty=1):
    from alpaca.trading.requests import MarketOrderRequest as MOR
    from alpaca.trading.enums import OrderSide, TimeInForce
    req = MOR(symbol=symbol, qty=qty, side=OrderSide.BUY, type='market', time_in_force=TimeInForce.DAY)
    return self.trade_client.submit_order(req)

def market_sell_qty(self, symbol, qty):
    req = MOR(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
    return self.trade_client.submit_order(req)

def get_account(self):
    return self.trade_client.get_account()
```

### Risk exclusion (core/state_manager.py)
Treasury proxies must NOT eat MAX_RISK 75k buying power:
```python
TREASURY_SYMBOLS = {"SGOV", "USFR", "BIL", "SHV", "TFLO"}
def calculate_risk(positions):
  for p in positions:
    if getattr(p,"symbol","") in TREASURY_SYMBOLS: continue
    ...
def calculate_exposures(positions): # returns put_exp, long_stock excl treasury, risk
```

### Strategy integration (scripts/run_strategy.py)
```python
def sync_sgov_real(client, logger):
    positions = client.get_positions()
    put_exp, long_stock, risk = calculate_exposures(positions)
    sgov_qty, sgov_price = get_current_sgov(positions)
    idle = TOTAL_CAPITAL - risk
    target_cash = min(50000, idle) if put_exp==0 and long_stock==0 else max(0, idle)
    target_shares = floor(target_cash / price)
    diff = target_shares - sgov_qty
    # Guard: check existing open BUY orders to avoid duplicate
    open_orders = client.trade_client.get_orders(filter=OPEN)
    sgov_open_buy = sum(qty for o in open_orders if o.symbol=='SGOV' and side==BUY)
    if sgov_open_buy>0 and diff>0: diff=0 # already queued
    if diff>0: client.market_buy("SGOV", diff)
```

Called after wheel puts, before Wheeler sync. Also skips CC selling on SGOV.

### WheelerSync idempotent (core/wheeler_sync.py)
Previous POST-only duplicated long_positions on each cron $49k→$99k. Fix DELETE then POST:
```bash
sg docker -c "docker exec wheeler sqlite3 /app/data/wheeler.db \"DELETE FROM long_positions WHERE symbol='SGOV';\""
```
Then POST. Same pattern for all equity sync via subprocess wrapper (sg docker needed for group perms).

### sync_sgov.py standalone
Now does REAL Alpaca execution + Wheeler sync:
- Fetch price via StockHistoricalDataClient latest trade fallback 100.72
- calc risk excl treasury, target floor(TargetCash/price)
- Open-order guard (prevents 992 qty bug from 2×496 duplicate submits Sunday)
- submit MarketOrderRequest, sleep 2, refresh qty
- If market closed and 0 filled but target>0, still record intended as Treasury proxy in Wheeler (shows cash intent, fills next open)
- sync_to_wheeler deletes via sqlite then POST

### Live state after fix
- Alpaca: 1×496 BUY ACCEPTED b8ed14b8 queued Monday (market closed Sunday), no duplicates after cancel
- Wheeler: LS $0 Put $0 Treas $49,957
- Cron run_wheel_cron.sh: run-strategy (wheel+SGOV real+Wheeler) + dynamic fallback
- Verification:
```bash
source .venv/bin/activate
python -c "from core.broker_client import BrokerClient ...; print(open orders)"
curl -s http://localhost:8096/api/allocation-data | jq
sg docker -c "docker exec wheeler sqlite3 /app/data/wheeler.db 'SELECT symbol,shares,buy_price FROM long_positions;'"
```

### Pitfalls
- Duplicate guard mandatory when cron runs twice quickly + market closed (ACCEPTED not FILLED, second run would submit again → 992). Always check OPEN orders before new BUY.
- Market closed Sunday → filled_qty=0 avg=None status ACCEPTED, positions still 0 until Monday open. Wheeler should still show intended.
- 600 perms from host cp → appuser can't read → 403 for css; same could affect template DB? Always chmod -R a+r after cp.
- Document order id and cancel duplicates via cancel_order_by_id if needed.
