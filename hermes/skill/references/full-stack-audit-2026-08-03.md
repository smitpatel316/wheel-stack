# Audit Findings — Full Stack 2026-08-03 Post Optionable Migration

## Snapshot Commands That Reveal All

```bash
sg docker -c "docker ps --format '{{.Names}} {{.Image}} {{.Ports}} {{.Status}}'"
df -h / && du -sh ~/optionable-data/ && ls -lh ~/optionable-data/
cat ~/.cloudflared/config.yml
crontab -l
cat ~/options-wheel/run_wheel_cron.sh
# Alpaca
cd ~/options-wheel && source .venv/bin/activate && python - <<'PY'
from core.broker_client import BrokerClient
from config.credentials import *
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
c=BrokerClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, IS_PAPER)
acct=c.trade_client.get_account()
print(acct.equity, acct.cash, acct.buying_power)
print(len(c.get_positions()))
PY
curl -s http://localhost:8096/api/health | jq
curl -s http://localhost:8096/api/accounts | jq
curl -s http://localhost:8096/api/trades | jq '.data | length'
curl -s http://localhost:8096/api/stocks | jq
curl -s http://localhost:8096/api/fund-transactions | jq
curl -s http://localhost:8096/api/portfolio/stats?accountId=1 | jq
curl -s http://localhost:8096/api/settings | jq
cat ~/options-wheel/config/params.py
cat ~/options-wheel/core/state_manager.py | grep -A5 TREASURY_SYMBOLS
cat ~/options-wheel/logs/cron.log | tail -50
```

## Gaps Found — 10 Fixed

### 1. DB file owned by root (docker run as root)
Symptom: backup `cp` permission, host `ls` root:root 124K. Future writes may fail if user deletes.
Fix: `sudo chown -R smitpatel316:smitpatel316 ~/optionable-data/*.db*`

### 2. Commission $0.66 vs $0 paper
Optionable default $0.66/contract, Alpaca paper $0. Actual P/L off by $0.65*trades.
Fix: `PUT /api/accounts/1 {"commissionPerContract":0}` + in `core/optionable_sync.push_trade_to_optionable` use `_commission_for_trade()` reading `IS_PAPER` → 0 if paper else 0.65.

### 3. Backup only cp, missed WAL
Optionable uses SQLite WAL (optionable.db-wal 816K). Plain `cp optionable.db` is stale.
Fix: `backup.sh` uses `sqlite3 $DB ".backup '$BACKUP_DIR/..."` to checkpoint WAL, then gzip, retain 30. If host sqlite3 missing, fallback cp + `docker exec optionable sqlite3 /data/optionable.db ".backup /data/..."` (needs tool inside image).

### 4. Equity sync duplicates every cron
Old `sync_alpaca_equity_to_optionable` did POST without dedup → 1 new lot per cron run.
Fix: fetch existing `GET /api/stocks?accountId`, map ticker→stock, skip if shares+costBasis same within 0.01, else DELETE before POST replace. Same pattern for SGOV: check existing qty/avg, skip if match, else delete all SGOV then POST. Idempotent.

### 5. No close handling — Open forever
Alpaca short option disappears on expiry/assignment/buy-to-close, but Optionable stayed Open.
Fix: `sync_closed_trades(client)`:
- `GET /api/trades?status=Open&accountId`
- Parse Alpaca OCCs `AAPL260905P00300000` → (ticker, strike, exp, Put/Call) set
- For each open trade key not in Alpaca set:
  - if ticker now in stock tickers and type CSP → Assigned
  - else if exp <= today → Expired else Closed
  - PUT `/api/trades/{id}` {status, closedDate=today, closePrice=0}
Run in `run_strategy` after equity/sgov sync and in extra safety block cron + in stream handler on expired/canceled/fill.

### 6. Cron log accumulated old Wheeler 404 warnings
Before cleanup, `logs/cron.log` had `Wheeler POST failed 404 Cannot POST /api/long-positions` from prior runs.
Fix: `> logs/cron.log` rotate + remove Wheeler legacy code.

### 7. sqlite3 binary missing on host
`sqlite3: command not found` — Pi dietpi minimal, libsqlite3 installed but not cli.
Fix: install `sqlite3` apt or fallback to cp + note docker exec alternative.

### 8. run_strategy logger signature broken after rewrite
Original: `setup_logger(level=..., to_file=...)`, `StrategyLogger(enabled=...)`.
Broken rewrite introduced `setup_logger(args, f"{strat_log_dir}/...")` AttributeError: `Namespace has no attr strat_log_dir`.
Fix: revert to original pattern from `git show HEAD~1`. Verify `./run_wheel_cron.sh` tail clean: `[Current buying power $75000] [No put options...] [SGOV]... [Synced positions to Optionable tracker]`.

### 9. SGOV qty check used string `lower()` on Enum side
Old `str(side).lower()` might be `OrderSide.BUY` → `'orderside.buy'` still contains buy but fragile.
Fix: `str(side).lower().find('buy')>=0` or `'buy' in str(side).lower()` already ok, but better `getattr(side,'value',str(side)).lower()`.

### 10. Cron extra sync missing closed trades
Cron ran `sync_alpaca_equity + sync_sgov` but not `sync_closed_trades`.
Fix: extra block now includes `sync_closed_trades(c)`.

## Remaining Minor (Accepted)

- Dividends/interest from SGOV not auto-tracked as fund_transaction dividend. Need `GET /v2/account/activities` DIV/INT → POST to `/api/fund-transactions`. Alpaca SDK method missing in 0.43.5, needs raw REST. Low priority paper.
- Alpaca margin `initial_margin 24977` on SGOV BUY — paper account margin enabled, not cash. Not harmful cash $100k covers. Could request cash account type from Alpaca broker support but paper default is margin.
- Optionable mobile Tailwind responsive works out-of-box, no hamburger fix needed unlike Wheeler (which needed 7607B mobile.css + inline @media double protection due to FileServer 403 0600 perms and CF HIT stale 19506).
- No webhook, depends on cron + stream. Stream service `alpaca-stream.service` enabled active, restart always, logs stream.log. Keep cron as fallback.
- No CI test for optionable_sync OCC parser — manually tested `AAPL260905P00030000` → CSP 30 correct, dup check via GET open.

## Final State After Fixes 2026-08-03 21:25 PDT

- API health tradeCount 0 v0.16.0, accounts 1 Alpaca Paper $100k commission 0, stocks 1 SGOV 496×100.72 id 11, fund 1 deposit 100k, trades 0 (Sunday closed)
- Alpaca equity 100k cash 100k BP 350k, positions 0, open 1 SGOV BUY 496 ACCEPTED, 1 canceled duplicate 992 bug fixed
- wheel.smitpatel.net + optionable.smitpatel.net → Optionable Portfolio SGOV 496 live price $100.71 -$4.96 Yahoo, Fund $100k, Deployed $0
- Docker optionable Up 43m healthy, compose 8096, tunnel pi-tunnel b826..., crontab 3 wheel + backup + cloudflared every 5m
- Stream service active PID, connected to BaseURL.TRADING_STREAM_PAPER, subscribed trade_updates, logs/stream.log
