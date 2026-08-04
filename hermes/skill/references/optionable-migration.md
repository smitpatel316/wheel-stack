# Wheeler → Optionable Migration — wheel.smitpatel.net 2026-08-03

## Why
Wheeler (Go tracker, March 2024, fixed sidebar 220px, non-responsive, manual template hacks, CGO build pain 38GB cache, overlay bug) → Optionable (React18+Vite+Tailwind+Recharts+Express+better-sqlite3 WAL, v0.16.0, trade chains CSP→stock→CC grouping, roll linking parentTradeId, portfolio mode fund journal deposits/withdrawals/dividends/interest/fees, RoR monthly stacked P/L, income donut, multi-account, commission $0.66/contract, dark mode, N/S/H/Esc, CSV multi-section dup detection, buy-side CALL/PUT live batch prices, mobile responsive out of box, MIT, 642MB arm64 multi-arch).

## Deploy Pi budupi ARM64
```bash
mkdir -p ~/optionable-data
docker pull yomikoye/optionable:latest  # 642MB digest 9160f2f multi-arch amd64|arm64
cat > ~/optionable-data/docker-compose.optionable.yml <<'YAML'
services:
  optionable:
    container_name: optionable
    image: yomikoye/optionable:latest
    ports: ["8098:8080"]  # 8097 occupied by market-dashboard python PID 518759 -> market.smitpatel.net
    environment: [TZ=America/Los_Angeles, NODE_ENV=production, DATA_DIR=/data]
    volumes: [/home/smitpatel316/optionable-data:/data]
    restart: unless-stopped
YAML
sg docker -c "docker compose -f ~/optionable-data/docker-compose.optionable.yml up -d"
# logs: [yahoo-finance2] Node >=22 warn but works, migrations v1..v14, seed 6 trades
curl -s http://localhost:8098/api/health | jq # {tradeCount:6 version:0.16.0}
```

## Port conflict pitfall 8097
`lsof -i :8097` → python /home/smitpatel316/market-dashboard/app.py market timing dashboard (Buffett/CAPE). Use 8098 temporary, then move to 8096 after wheeler stop.

## Tunnel 2-step mandatory
1. Ingress ~/.cloudflared/config.yml:
```yaml
- hostname: wheel.smitpatel.net
  service: http://localhost:8096
- hostname: optionable.smitpatel.net
  service: http://localhost:8096
```
`sudo systemctl restart cloudflared`
2. DNS CNAME:
```bash
cloudflared tunnel route dns pi-tunnel wheel.smitpatel.net
cloudflared tunnel route dns pi-tunnel optionable.smitpatel.net
```
Without step 2 → ERR_NAME_NOT_RESOLVED. Wait 60s, `curl -sI https://wheel.smitpatel.net/ → 200`.

## Migration to wheel.smitpatel.net
```bash
sg docker -c "docker stop wheeler; docker rm wheeler"  # keep image wheeler:pi rollback
sed -i 's/8098:8080/8096:8080/' ~/optionable-data/docker-compose.optionable.yml
sg docker -c "docker compose -f ~/optionable-data/docker-compose.optionable.yml up -d"
curl -s http://localhost:8096/api/health # Optionable now
mv ~/wheeler ~/wheeler-archived-$(date +%Y%m%d) # 29M db 116K
```

## Sync adapter core/optionable_sync.py
- OCC parser: r'^([A-Z]+)(\d{6})([PC])(\d{8})$'  AAPL260905P00030000 -> AAPL, 2026-09-05, P, $30, YY<70 -> 20YY
- Type mapping: short put -> CSP, short call -> CC (wheel), long CALL/PUT not used yet
- POST /api/trades {ticker, type, strike dollars, quantity, delta, entryPrice per-share dollars, closePrice 0, openedDate YYYY-MM-DD, expirationDate, accountId, commission 0.65*qty}
  Storage INTEGER cents, API accepts dollars -> conversions.js toCents
  Dup check via /api/trades import logic: ticker+type+strike+qty+entryPrice+opened+expiration+accountId
- sync_alpaca_equity_to_optionable: POST /api/stocks {ticker, shares, costBasis, acquiredDate, accountId}
- sync_sgov_to_optionable: Treasury proxy SGOV 496x100.72 -> stock table
  Idempotent: GET /api/stocks?accountId, DELETE /api/stocks/{id} for SGOV, then POST
- SGOV open-order guard: check OPEN BUY orders before new BUY, skip if 496 already queued (fixes 992 duplicate bug Sunday market closed ACCEPTED status not FILLED)

## Initial seeding
```bash
# delete seed 6 trades
for id in $(curl -s http://localhost:8096/api/trades | jq -r '.data[].id'); do curl -s -X DELETE http://localhost:8096/api/trades/$id; done
curl -X PUT /api/accounts/1 -d '{"name":"Alpaca Paper $100k"}'  # default commission $0.66 preserved
curl -X POST /api/fund-transactions -d '{"accountId":1,"type":"deposit","amount":100000,"date":"2026-08-02","description":"Initial Alpaca Paper funding"}'
curl -X POST /api/stocks -d '{"accountId":1,"ticker":"SGOV","shares":496,"costBasis":100.72,"acquiredDate":"2026-08-02","notes":"Treasury proxy"}'
curl -X PUT /api/settings/portfolio_mode_enabled -d '{"value":"true"}'
curl -X PUT /api/settings/dark_mode -d '{"value":"true"}'
curl -X PUT /api/settings/confirm_expiry -d '{"value":"false"}'
```

## Integration
- core/execution.py: after market_sell -> push_trade_to_optionable + optional wheeler push wrapped try
- scripts/run_strategy.py: sync_sgov_real real MarketOrderRequest SGOV via Alpaca (Treasure symbols excluded from risk, 50k rule, target floor), then optionable_sync alive -> sync_alpaca_equity_to_optionable + sync_sgov_to_optionable
- run_wheel_cron.sh: healthcheck /api/health not /api/allocation-data, restart compose, run-strategy, sync_sgov dynamic, extra safety sync block
- Cron ET 10:05/13:05/15:35 still

## Verification
- curl http://localhost:8096/api/health -> tradeCount 0 version 0.16.0
- curl http://localhost:8096/api/stocks -> 1 SGOV 496
- curl http://localhost:8096/api/portfolio/stats?accountId=1 -> netDeposited 100k
- Browser https://wheel.smitpatel.net/?v=final_migrated -> Optionable v0.16.0, All Accounts / Alpaca Paper $100k, Options tab 0 chains, Portfolio tab: Deposited $100k, Fund Journal 1 txn, Stock Positions 1 tickers 1 lots SGOV 496 $100.72 avg $100.71 live -$4.96 (Yahoo live), Buy Stock button
- Push test: push_trade_to_optionable("AAPL260905P00030000",0.85,1,delta=0.25) -> trade id CSP 30 $0.85 Open, stats totalPremium 205 PnL 203.7 after commission, then DELETE cleanup
- Alpaca: equity 100k cash 100k, open SGOV BUY 496 ACCEPTED b8ed14b8 queued Monday (market closed Sunday)

## Real SGOV execution (already in sgov-real-alpaca-sync.md)
- BrokerClient market_buy/sell_qty/get_account
- TREASURY_SYMBOLS excluded from risk
- Duplicate guard: check OPEN orders before new BUY, fixes 992 bug
- Live: 1x496 ACCEPTED after cancel duplicate, Wheeler Treas 49957 intact before removal

## Rollback
Image wheeler:pi f441ff04abf9 130MB archived, db ~/wheeler-archived-20260802/wheeler.db 116K, compose quick restore if needed.
