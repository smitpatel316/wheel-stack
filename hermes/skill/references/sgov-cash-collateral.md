# SGOV as Cash Collateral — Session 2026-08-02

## Context
User asked: Does Wheeler buy treasuries directly or via ETFs like SGOV? Then: assume we bought SGOV with extra cash.

## Original Model (direct CUSIP)
- Table `treasuries(cuspid TEXT PK, purchased DATE, maturity DATE, amount, yield, buy_price, current_value, exit_price)`
- CUSPID typo for CUSIP — Committee on Uniform Securities Identification Procedures, 9-char issuance ID, e.g. `912797JX2`
  - `912797`=US T-Bill issuer, `JX`=maturity/coupon, `2`=check digit
- Flow: Treasury amount is collateral for CSPs. Example from wheel_strategy_example.sql:
  - Buy `('912797JX2','2025-01-01','2025-12-31',75000,4.83,74700)` — $75k T-Bill 4.83%
  - Put assigned: `UPDATE treasuries SET amount = amount - 9500 WHERE cuspid='912797JX2'` (liquidate to buy 100 shares)
  - Call away: `amount += 10200`
  - Interest quarterly: `('INT-Q1-2025','2025-03-31','2025-03-31',857.19,5.25,100)` synthetic entry
- Dashboard: Treasuries page input CUSPID, Purchased, Maturity, Amount, Yield, Buy Price, Current Value, Exit Price. Charts: Bonds Held, Leverage Over Time, Current Leverage Gauge. Metrics history.

## SGOV Proxy Model (adopted)

### Why
- SGOV = iShares 0-3 Month Treasury Bond ETF, $100.72 on 2026-07-31, div ~$0.42/qtr, yield ~5.1% mirroring Fed Funds, 0.07% expense, highly liquid, no CUSIP ladder maintenance.
- Same yield exposure as direct T-Bills without managing individual CUSIPs, maturities, roll schedule.
- User already holds Alpaca paper $100k, 50% cash rule → $50k idle cash baseline.

### Target Allocation
- Baseline: 496 shares = floor(50000 / 100.72) = $49,957.12 = Treasuries slice in dashboard
- Dynamic (future): `idle = TOTAL_CAPITAL (100k) - putExposure - longStockNonSGOV`, clamp 0..TOTAL, floor $5k min for yield visibility.

### Implementation in Wheeler (Go)

**File: internal/web/dashboard_handlers.go (patched 2026-08-02)**
```go
var cashCollateralSymbols = map[string]bool{"SGOV": true, "USFR": true, "BIL": true, "SHV": true}

func buildTotalAllocationChart(longPositions []*LongPosition, options []*Option, totalTreasuries float64) []ChartData {
  var totalLong, totalPuts, sgovAsTreasury float64
  for _, pos := range longPositions {
    if pos.Closed == nil {
      if cashCollateralSymbols[pos.Symbol] {
        sgovAsTreasury += pos.CalculateAmount()
      } else {
        totalLong += pos.CalculateAmount()
      }
    }
  }
  treasuryEquivalent := totalTreasuries + sgovAsTreasury
  return []ChartData{
    {Label: "Long Stock", Value: totalLong, Color: "#36A2EB"},
    {Label: "Put Exposure", Value: totalPuts, Color: "#FF6384"},
    {Label: "Treasuries", Value: treasuryEquivalent, Color: "#FFCE56"},
  }
}
```

Similar patch in:
- `buildDashboardData`: computes sgovValue loop, `treasuryEquivalent := totalTreasuries + sgovValue`, passes to `calculateDashboardTotals` so GrandTotal includes SGOV.
- `allocationDataHandler`: same sgovAsTreasury split, log line updated to `"Treasuries (incl SGOV): $%.2f (direct $%.2f + SGOV $%.2f)"`, longByTicker still includes SGOV for detail but TotalAllocation moves it to Treasuries.

Result:
```
Before: Long $49957, Treasuries $0
After:  Long $0, Treasuries $49957.12
```

### Sync Script: scripts/sync_sgov.py

- Price via Alpaca `StockHistoricalDataClient`, `StockLatestTradeRequest(symbol=SGOV)`
- Allocation via `GET /api/allocation-data`
- Target shares = floor(target_cash / price)
- **Critical delete-before-insert**: Host SQLite `sqlite3.connect(db_path)` fails when container holds WAL lock → `attempt to write a readonly database`. Direct `docker exec` without `sg` wrapper fails group perms (user not in docker group, needs `sg docker -c`). Correct pattern:
```bash
sg docker -c "docker exec wheeler sqlite3 /app/data/wheeler.db \"DELETE FROM long_positions WHERE symbol='SGOV';\""
```
  - Wrong pattern `DELETE FROM long_positions WHERE symbol=\"SGOV\";` (double quotes inside SQL) → `no such column: SGOV` because SQLite interprets double-quoted string as identifier. Must single-quote `'SGOV'`.
- Recreate via API: `PUT /api/symbols/SGOV` {price, dividend:0.42} + `POST /api/long-positions` {symbol,shares,buy_price,opened}
- Run twice → should stay $49,957 not $99k/149k. Doubling symptom means delete failed.

**File duplication bug during session**: script initially used `subprocess.run(["docker","exec",...])` — silent fail due to permissions, leading to accumulation $49k→99k→149k→199k. Fixed by `sg docker -c "..."` wrapper.

### Cron Integration

`run_wheel_cron.sh`:
```bash
run-strategy --strat-log ...
python3 scripts/sync_sgov.py 50000   # or dynamic
python3 -c "... sync_alpaca_equity_positions_to_wheeler ..."
```

### Verification

```bash
curl -s https://wheel.smitpatel.net/api/allocation-data | jq
# Expect totalAllocation Treasuries ~49957 when clean
sg docker -c "docker exec wheeler sqlite3 /app/data/wheeler.db 'SELECT symbol,shares,buy_price FROM long_positions;'"
# Should be single row SGOV|496|100.72
```

### Docker Build Reliability Notes

- Pi root 117G 78G used 35G avail, build cache 38GB, images 15G. Low space causes `snapshot does not exist` during `RUN apk add gcc musl-dev`.
- Reliable bypass: pre-build binary outside image:
```bash
docker run --rm --entrypoint sh -v /home/smitpatel316/wheeler:/work -w /work golang:1.24-alpine -c 'apk add --no-cache gcc musl-dev; CGO_ENABLED=1 go build -o /work/wheeler.new .'
docker cp wheeler.new wheeler:/app/wheeler.new && docker exec wheeler sh -c 'mv /app/wheeler.new /app/wheeler && chmod +x /app/wheeler'
docker restart wheeler
docker commit wheeler wheeler:pi
```
- Always commit after binary injection so restart keeps SGOV patch.

### Live State 2026-08-02 EOD

- Container `wheeler:pi` (aff1ab / 8d6f97) Up, :8096, commit includes SGOV patch
- Allocation API: `{Long:0, Put:0, Treasuries:49957.12, longByTicker:[{SGOV:49957.12}]}`
- Public: https://wheel.smitpatel.net/title=Dashboard - Wheeler, JSON treasuries 49k
- Positions DB: single SGOV row after cleanup
