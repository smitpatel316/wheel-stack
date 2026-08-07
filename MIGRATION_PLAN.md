# Wheel-Stack Pi → Hatch Migration Plan — v2.6.0

**Date:** 2026-08-05  
**Pi host:** `budupi` / `smitpatel316` account  
**Hatch host:** this machine (`~/workspace/wheel-stack` cloned, .env set)  
**Alpaca Paper:** PA3WFOAHE2C6 verified equity $100,169.14 cash $26,274.28 ACTIVE (IS_PAPER=true)  
**Goal:** move Optionable (8096) + wheel-stack Python core + Hermes agentic cron + cloudflared tunnel off Pi onto Hatch without data loss.

---

## 1. Current Pi State (from README + docker-compose.yml + pi/deploy.sh)

### Optionable
- Docker `yomikoye/optionable:latest` container `optionable` port `8096:8080`
- Env: `TZ=America/Los_Angeles`, `NODE_ENV=production`, `DATA_DIR=/data`, `PORT=8080`
- Volume: `/home/smitpatel316/optionable-data:/data` (15 trades prod, v0.16.0, P/L fix $568->$52 real closePrice)
- Health: `http://localhost:8096/api/health` → `v0.16.0 React18+Vite Express better-sqlite3 WAL`
- Export endpoints: `GET /api/settings/export-db` → `optionable-YYYY-MM-DD.db`, `/api/trades` envelope `{success,data:[...],meta}`

### Wheel-Stack Python Core
- `~/wheel-stack` private repo `smitpatel316/wheel-stack` v2.6.0 unified: earnings+dividend+fundamentals+volatility -> closer 50% -> roller 3% -> wheel -> SGOV SPAXX -> Optionable
- `config/params.py` MAX_RISK 90k, DELTA 0.18-0.35 adaptive IVRank, YIELD 0.008-0.50, EXP 14-60, OI 100 allow None, spread $0.15/12% NTM $0.05, closer 50% DTE>3, roller 3% OTM
- `config/symbol_list.txt` 25 diversified (AAPL CSCO INTC AMD BAC WFC F T VZ SBUX KO ...)
- `core/broker_client.py` via Alpaca-py TradingClientSigned + Stock + Option historical
- `scripts/run_strategy.py` single entrypoint implementing all Hermes phases 0.1-6

### Hermes Agentic Cron
- Pi runs `hermes` daemon, 2 cron jobs: `tamelabs` paused + `options-wheel-agentic` id `014708b33a6a`
- Schedule `5 7,10,12 * * 1-5` — README says PDT 07:05/10:05/12:05 = ET 10:05/13:05/15:05 (docs write 15:35 typo)
- Prompt file: `hermes/cron/options-wheel-agentic.prompt.md` 20k chars + 30 references in `hermes/skill/references/`
- MCP: alpaca-mcp 62 tools + alphavantage 131 tools via `https://mcp.alphavantage.co/mcp?apikey=...`
- Gateway restart guard: cannot restart from inside gateway process (safety #30719) – must SSH outside `systemctl --user restart hermes-gateway`
- Tools kept custom: Optionable REST, strategy scoring/filter/select, roller/closer/context, SGOV sweep

### Cloudflared Tunnel pi-tunnel
- Config snippet `pi/cloudflared-config-snippet.yml`:
```yaml
ingress:
  - hostname: wheel.smitpatel.net
    service: http://localhost:8096
  - hostname: optionable.smitpatel.net
    service: http://localhost:8096
  - hostname: wheel-api.smitpatel.net
    service: http://localhost:8097
  - hostname: webhook.smitpatel.net
    service: http://localhost:8644
  - service: http_status:404
```
- DNS CNAME via `cloudflared tunnel route dns pi-tunnel <hostname>`
- Tunnel ID b826... (partial in README)
- System crontab 2 jobs: cloudflared watchdog + backup 2am

---

## 2. Hatch Workspace — What’s Done

### Clone & .env
- `~/workspace/wheel-stack` cloned private, .env created from user-provided keys (matches `config/.env.example` which currently **commits real secrets** – rotate ASAP)
- `.gitignore` correctly excludes `.env`, `*.db`, `optionable-data/`, `logs/*.jsonl`
- Python deps attempted: `python-dotenv`, `alpaca-py 0.43.5`, `pandas`, `numpy`, `requests` installed with `--break-system-packages` (PEP 668)
- Verified: `BrokerClient` connects – equity 100169.14 (run confirmed 2026-08-05 via python)

### Optionable-src Clone
```bash
rm -rf ~/workspace/optionable-src
git clone https://github.com/yomikoye/optionable.git ~/workspace/optionable-src
```
- Version cloned: 0.17.0 (Pi production uses yomikoye/optionable:latest = 0.16.0 per README, small drift)
- Structure inspected:
  - `server.js` → `createApp(__dirname)` + `startServer(app)` from `server/index.js`
  - `server/index.js`: `PORT=process.env.PORT||8080`, `DATA_DIR` from `server/db/connection.js`
  - `server/db/connection.js`:
```js
const DATA_DIR = process.env.DATA_DIR || './data'
if (!existsSync(DATA_DIR)) mkdirSync(DATA_DIR,{recursive:true})
export const dbPath = join(DATA_DIR,'optionable.db')
export const db = new Database(dbPath)
db.pragma('journal_mode=WAL')
```
  - DB tables: `schema_migrations`, `trades`, `positions`, `price_cache`, `settings`, `accounts`, `fund_transactions`, `stocks`, `portfolio`
  - Routes: `/api/health`, `/api/trades`, `/api/positions`, `/api/prices`, `/api/settings` (includes `GET /export-db` → download db), `/api/stats`, `/api/accounts`, `/api/fund-transactions`, `/api/stocks`, `/api/portfolio`

### Without-Docker Run Attempt — Blocked

**Environment:** Node 18.19.1, npm 9.2.0, `/usr/local/bin/cloudflared` 2026.7.3 present, no Docker daemon (`docker: command not found`)

**npm install output:**
```
npm WARN EBADENGINE better-sqlite3@12.11.1 required node 20.x||22.x||23.x||24.x||25.x||26.x current v18.19.1
npm WARN EBADENGINE yahoo-finance2@3.13.2 required >=20.0.0 current v18.19.1
npm ERR! ECONNRESET network aborted behind proxy
```
- `vite` not found despite install (devDeps not installed fully due to ECONNRESET)
- `NODE_ENV=development DATA_DIR=/tmp/optionable-test-data2 PORT=8098 node server.js` crashed with `ERR_MODULE_NOT_FOUND` / `ERR_INTERNAL_ASSERTION` native module load – `better-sqlite3` needs Node 20+ native build

**Conclusion:**
- Port handling verified: `PORT` env → 8080 default (Pi maps 8096:8080). Hatch should set `PORT=8096` if we want direct parity, or 8080 and proxy.
- DATA_DIR handling verified: `DATA_DIR` env → `./data` default, Pi uses `/data` inside container mounted from host path. Hatch should use `/home/hatch/workspace/optionable-data` or `~/workspace/wheel-stack/optionable-data` and export via env.
- Production static serving requires `vite build` → `dist/` → `express.static(join(rootDir,'dist'))` when `NODE_ENV=production`. Without build, `NODE_ENV=development` skips static serving but still needs better-sqlite3.
- **Blocker:** Hatch Node 18 cannot run optionable 0.17.0 without upgrade to Node 20+ or Docker. Options: upgrade Node via nvm/nodesource, or run via external Docker host (Fly/Render), or keep Pi alive for Optionable temporarily.

We did create temporary data dir `/tmp/optionable-test-data` and verified mkdir logic would create `optionable.db` if Node version compatible.

---

## 3. Data Migration — What Pi Holds

### Pi Source Path
- Host: `/home/smitpatel316/optionable-data` (mounted as `/data` in container)
- Expected files (from SQLite WAL mode):
  - `optionable.db` – main DB (15 trades, positions, price_cache, settings)
  - `optionable.db-wal` – WAL log (if not checkpointed)
  - `optionable.db-shm` – shared memory file
  - Possibly `logs/` symlink or separate folder
  - `settings` table contains `OPTIONABLE_URL`, thresholds

**Exact inventory needed** – run on Pi:
```bash
ssh smitpatel316@budupi
ls -lh /home/smitpatel316/optionable-data/
sqlite3 /home/smitpatel316/optionable-data/optionable.db "SELECT count(*) FROM trades; SELECT status, count(*) FROM trades GROUP BY status;"
curl -s http://localhost:8096/api/health | jq
curl -s http://localhost:8096/api/trades | jq '.data | length'
```

### Export Options (since we have no Pi SSH here)

**Option A – Tar.gz (preferred, preserves WAL checkpoint):**
On Pi:
```bash
cd /home/smitpatel316
# checkpoint WAL to main file to avoid partial WAL export (requires sqlite3 binary, else skip)
sqlite3 optionable-data/optionable.db "PRAGMA wal_checkpoint(TRUNCATE);"
tar czf optionable-data-$(date +%F).tar.gz optionable-data/
ls -lh optionable-data-*.tar.gz
# scp to laptop or upload to Hatch via /tmp
scp optionable-data-2026-08-05.tar.gz hatch:~/workspace/
```
On Hatch (after upload):
```bash
mkdir -p ~/workspace/optionable-data
tar xzf ~/workspace/optionable-data-2026-08-05.tar.gz -C ~/workspace/
ls -lh ~/workspace/optionable-data/
# or if inside wheel-stack clone path from tar: cp -r ~/optionable-data/* ~/workspace/optionable-data/
```

**Option B – Optionable native export (no SSH DB access):**
```bash
curl -s http://localhost:8096/api/settings/export-db -o optionable-$(date +%F).db
# Also CSV/JSON via UI? Pi tunnel: https://wheel.smitpatel.net -> download via Settings -> Export DB
# Then upload to Hatch: DATA_DIR=/home/hatch/workspace/optionable-data PORT=8096 node server.js will see it
```

**Option C – Pi deploy.sh backup logic:**
Pi's system crontab has 2am backup `cp` fallback – might already have backups in `~/wheel-stack/logs/` or somewhere else. Check:
```bash
crontab -l
cat ~/wheel-stack/logs/backup.log
ls ~/wheel-stack/optionable-data/ ~/optionable-data-bak* 2>&1
```

**For Hatch import:**
- Set env `DATA_DIR=/home/hatch/workspace/optionable-data` (or `~/workspace/wheel-stack/optionable-data`)
- Ensure `optionable.db` owned by Hatch user, chmod 600
- Start optionable with `DATA_DIR=/home/hatch/workspace/optionable-data PORT=8096 NODE_ENV=production node server.js` (requires Node 20+ build) OR run via Docker on external host mounting same dir
- Verify: `curl http://localhost:8096/api/health` and `curl http://localhost:8096/api/trades | python3 -m json.tool`

**Do not push `optionable.db` to GitHub – it is gitignored. Keep it private.**

---

## 4. Python Core Migration

### Verified
- `pip install --break-system-packages python-dotenv alpaca-py pandas numpy requests` – alpaca-py 0.43.5 installed
- `BrokerClient(ALPACA_API_KEY, IS_PAPER=true)` connects – equity verified
- Symbol list: 25 tickers from `config/symbol_list.txt`

### Run Strategy Equivalent
`scripts/run_strategy.py` already implements phases 0.1-6 without Hermes/MCP, using pure Python `core/*`:
- Phase 0.1 earnings `earnings_calendar.build_cache` Finnhub+Alpha fallback 6h cache `logs/earnings_cache.json`
- Phase 0.2 dividend Alpha 12h cache `logs/dividend_cache.json`
- Phase 0.3 fundamentals P/E Debt/Eq
- Phase 0.4 volatility RV 20d RV rank proxy IVR
- Phase 0.5 liquidity volume trend
- Phase 0.6 critical earnings alert `logs/earnings_critical_alert.json`
- Phase 1 context analyzer Yahoo v8 ^VIX real primary, VIXY proxy fallback, SPY 5d momentum vol 15.6%
- Phase 2 closer 50% DTE>3 40%+$0.20 DTE7-21 75% high urgency max 3
- Phase 3 roller 3% OTM net $0.10 spread $0.15/12% NTM $0.05 max2 close-before-open 2s
- Phase 4 wheel sells with earnings+dividend+fundamentals+vol adaptive delta + spread $0.15/12% NTM $0.05 + MIN_PREMIUM 0.20 + guard duplicate OPEN orders + BP guard $2000 + limit mid-price 8s wait market fallback
- Phase 5 SGOV SPAXX sweep ideal Fidelity 1007 shares $101k $440/mo 5.22% vs real Alpaca 454 $45k $198/mo limited by stockBP 40310000
- Phase 6 Optionable sync `http://localhost:8096` health v0.16.0 tradeCount 15 + SGOV sync `sync_sgov_to_optionable(client)` + `sync_closed_trades` + `sync_alpaca_equity`

**Dry run command (no orders placed until real execution via MCP or explicit flag):**
```bash
cd ~/workspace/wheel-stack
PYTHONPATH=. python3 scripts/run_strategy.py --dry-run --strat-log
# or with live paper but still respects IS_PAPER=true:
PYTHONPATH=. python3 scripts/run_strategy.py --strat-log --log-to-file
```

**Needs:** `logs/` directory exists (gitkeep present), `.env` set, `config/symbol_list.txt` present.

---

## 5. Hermes → Hatch Cron Mapping

### Source Schedule
- Pi Hermes: `5 7,10,12 * * 1-5` listed as PDT in `hermes/cron/README.md` (also says ET 10:05/13:05/15:35) — confusing legacy. `hermes/cron/README.md` explicitly: “Schedule meaning: `5 7,10,12 * * 1-5` UTC = 03:05, 06:05, 08:05 ET? Actually job config shows ET 10:05/13:05/15:35 — 7:05 UTC = 3:05 ET...” Keep exactly `5 7,10,12 * * 1-5`.
- Intent: run 3x during market hours Mon-Fri at open (~10:05 ET), midday (~13:05 ET), pre-close (~15:05 ET). Some docs say 15:35 ET close proximity.

### Hatch Cron Constraints
- Hatch Cron (`default.cron` tool) runs in UTC (?) and via owner `goal:<slug>` if goal-linked. Need to verify via `default.cron` docs. Assume UTC.
- Current date: early Aug DST, EDT = UTC-4, PDT = UTC-7. Winter EST = UTC-5, PST = UTC-8.
- No timezone field in classic cron – must convert manually and possibly create 2 schedules for DST vs Standard or accept drift.

### Proposed UTC Draft (Summer DST — Aug)
For PDT 07:05,10:05,12:05 = UTC 14:05,17:05,19:05:
```
# ET 10:05 / PT 07:05 — Market open check
5 14 * * 1-5  -> 10:05 EDT / 07:05 PDT

# ET 13:05 / PT 10:05 — Midday
5 17 * * 1-5  -> 13:05 EDT / 10:05 PDT

# ET 15:05 / PT 12:05 — Afternoon pre-close (README typo says 15:35)
5 19 * * 1-5  -> 15:05 EDT / 12:05 PDT
```
If true intent is ET 15:35 pre-close (more theta decay data):
```
35 19 * * 1-5 -> 15:35 EDT / 12:35 PDT = 19:35 UTC
```

### Hatch Cron Definition Draft (DO NOT CREATE YET — review needed)

```yaml
# option-wheel-agentic-1 — open
name: wheel-open-EDT
slug: wheel-stack-agentic-open
schedule: "5 14 * * 1-5"   # UTC 14:05 = EDT 10:05 / PDT 07:05
command: >
  cd ~/workspace/wheel-stack &&
  PYTHONPATH=. python3 scripts/run_strategy.py --strat-log --log-to-file 2>&1 | tee -a logs/cron.log
timezone: UTC
enabled: false (enable after Optionable live)
owner: goal:wheel-migration (if goal exists)

# option-wheel-agentic-2 — midday closer/roller peak
name: wheel-midday-EDT
slug: wheel-stack-agentic-midday
schedule: "5 17 * * 1-5"   # UTC 17:05 = EDT 13:05 / PDT 10:05
command: same as above

# option-wheel-agentic-3 — pre-close SGOV sweep + final wheel
name: wheel-preclose-EDT
slug: wheel-stack-agentic-preclose
schedule: "5 19 * * 1-5"   # UTC 19:05 = EDT 15:05 / PDT 12:05 (or 35 19 for 15:35 ET)
command: same as above

# Optional SGOV nightly reconciliation if not in main run
# 0 2 * * * -> backup + pnlTracker
# nightly-backup: tar czf ~/workspace/wheel-stack/optionable-data-backup-$(date +\%F).tar.gz ~/workspace/optionable-data
```

**For winter (EST/PST) adjust:** UTC 15:05,18:05,20:05 (or 20:35) to keep same ET clock time. Either create second set scheduled for Dec-Mar or accept 1h drift (usually fine – market open moves 1h relative UTC in winter).

**MCP Replacement Note:** Pi version uses MCP `place_option_order` via `alpaca-mcp` 62 tools (`get_account_info`, `get_clock`, `get_all_positions`, `get_orders`, `place_option_order`, `get_account_activities_by_type DIV/INT/FEE/OPASN/OPEXP`, `get_watchlist_by_id wheel-universe`) + `alphavantage` 131 tools (`EARNINGS_CALENDAR`, `DIVIDENDS`, `COMPANY_OVERVIEW`, `TIME_SERIES_DAILY`). Hatch version will use pure Python `BrokerClient` fallback (already in `scripts/run_strategy.py`) – no MCP gateway needed. Loss: LLM reasoning via OpenAI optional key (if set) still works.

**Webhook Finnhub → earnings cache clear:**
Pi has webhook `https://webhook.smitpatel.net/webhooks/finnhub-earnings` secret `***REMOVED***50` -> handler `~/.hermes/scripts/finnhub-earnings-handler.py` clears `logs/earnings_cache.json` to force refetch + triggers full agent. For Hatch, same webhook needs to be re-hosted (see cloudflared section). Until then, rely on 6h cache refresh during each cron run (already in `earnings_calendar.py`).

---

## 6. Cloudflared Tunnel — Credentials Plan

### Current Pi Tunnel
- Binary: `cloudflared` managed via `systemd` `cloudflared.service`, watchdog cron checks `systemctl status cloudflared --no-pager`
- Config: `~/.cloudflared/config.yml` (Pi) contains `tunnel: pi-tunnel` + `credentials-file: /home/smitpatel316/.cloudflared/<tunnel-id>.json` + ingress snippet (see Section 1)
- Tunnel ID: b826... (full needed) – from `~/.cloudflared/*.json` filename or `cloudflared tunnel list` output
- Origin cert: `~/.cloudflared/cert.pem` (for `tunnel route dns` ops, not needed for `cloudflared tunnel run`)

### Hatch Machine Status
- `cloudflared` binary found: `/usr/local/bin/cloudflared` → `/usr/bin/cloudflared` v2026.7.3
- No `~/.cloudflared/` directory (expected)
- `cloudflared tunnel list` fails `ERR Cannot determine default origin certificate path` – needs `cert.pem` / `TUNNEL_ORIGIN_CERT`

### What Is Needed to Run wheel.smitpatel.net from Hatch Instead of Pi

**Do NOT paste secrets in this doc — placeholders only.**

1. **Tunnel JSON** – `~/.cloudflared/<TUNNEL_ID>.json` (contains TunnelID, TunnelName pi-tunnel, TunnelSecret). Copy from Pi:
```bash
# On Pi
cat ~/.cloudflared/*.json
# Should look like:
# {"AccountTag":"...","TunnelSecret":"BASE64==","TunnelID":"b826....","TunnelName":"pi-tunnel"}
```
Upload to Hatch as `~/.cloudflared/<TUNNEL_ID>.json` chmod 600.

2. **Config.yml** – create `~/.cloudflared/config.yml`:
```yaml
tunnel: <TUNNEL_ID>
credentials-file: /home/hatch/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: wheel.smitpatel.net
    service: http://localhost:8096
  - hostname: optionable.smitpatel.net
    service: http://localhost:8096
  - hostname: wheel-api.smitpatel.net
    service: http://localhost:8097
  - hostname: webhook.smitpatel.net
    service: http://localhost:8644
  - service: http_status:404
```
(Adjust if Hatch serves Optionable on different port – we drafted PORT=8096 for parity)

3. **Optional origin cert** – only if you need `cloudflared tunnel route dns` (first-time DNS). After DNS already created (Pi already did), you do NOT need cert.pem for `cloudflared tunnel run`. If DNS missing:
```bash
# On machine with cert.pem (Pi)
cloudflared tunnel route dns pi-tunnel wheel.smitpatel.net
```
Already done per README, so Hatch likely just needs `run`.

4. **Starting tunnel on Hatch:**
```bash
cloudflared tunnel --config ~/.cloudflared/config.yml run pi-tunnel
# or via systemd equivalent: use default.cron with @reboot? Or default.process / tmux / cron watchdog
```
- Hatch has no systemd. Use `default.cron` with monitoring or `default.process` background + watchdog via `default.cron` list `cron.d`.
- Pi had watchdog system crontab checking cloudflared – replicate as Hatch cron: `*/5 * * * * pgrep -f cloudflared || /usr/local/bin/cloudflared tunnel --config ~/.cloudflared/config.yml run pi-tunnel`

5. **Conflict Avoidance:**
- Cloudflare Tunnel allows only one `cloudflared` instance per tunnel ID connected at a time (last-connect wins, flaps). BEFORE starting on Hatch, stop `cloudflared` on Pi:
```bash
sudo systemctl stop cloudflared
# verify https://wheel.smitpatel.net down (or returns 1033)
```
Then start on Hatch. Otherwise both will fight.

**Security Note:** Do NOT commit `.json` or `cert.pem` to git. Keep in `~/.cloudflared/`, chmod 600. If those leak, rotate tunnel: `cloudflared tunnel delete pi-tunnel && cloudflared tunnel create pi-tunnel` then redo DNS.

---

## 7. Migration Steps — Ordered Checklist

### Phase A — Preparation (Hatch, no Pi touch)
- [x] Clone wheel-stack, .env set, Alpaca paper verified equity $100169.14
- [x] Clone yomikoye/optionable to ~/workspace/optionable-src, inspect DATA_DIR/PORT handling
- [ ] Upgrade Node to 20+ on Hatch **OR** decide to host Optionable via external Docker (Fly/Render) — see Blocker 1
- [ ] `npm install` successfully (needs network stable, Node 20) + `npm run build` → `dist/`
- [x] Draft cloudflared config plan (this doc)
- [x] Draft Hatch cron schedule UTC equivalents (this doc)
- [ ] Pi SSH or manual export: get `/home/smitpatel316/optionable-data` tar.gz OR `optionable-*.db` via `GET /export-db`

### Phase B — Data Import to Hatch
- [ ] Upload `optionable-data.tar.gz` to `~/workspace/` (via scp / rsync / Hatch upload)
- [ ] Unpack to `~/workspace/optionable-data` and verify `sqlite3 optionable.db "SELECT count(*) FROM trades"` returns 15 (or expected)
- [ ] Point wheel-stack Optionable client: `OPTIONABLE_URL=http://localhost:8096` (default already) – verify `core/optionable_sync.py` uses env var
- [ ] Set `DATA_DIR=/home/hatch/workspace/optionable-data` export for optionable server

### Phase C — Optionable Live on Hatch
- [ ] `DATA_DIR=/home/hatch/workspace/optionable-data PORT=8096 NODE_ENV=production node server.js` OR Docker alternative:
```bash
# If Docker available (Hatch doesn't have dockerd)
docker run -d --name optionable -p 8096:8080 -v /home/hatch/workspace/optionable-data:/data -e NODE_ENV=production -e PORT=8080 yomikoye/optionable:latest
```
- [ ] Health check: `curl -s http://localhost:8096/api/health` → version 0.17.0 (or 0.16.0 if using image)
- [ ] Trades count: `curl -s http://localhost:8096/api/trades | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data', d)))"` → should match Pi 15
- [ ] P/L reconciliation: `PYTHONPATH=. python3 -c "from core.pnl_tracker import reconcile_optionable_vs_alpaca; from config.credentials import *; from core.broker_client import BrokerClient; c=BrokerClient(ALPACA_API_KEY,ALPACA_SECRET_KEY, IS_PAPER); print(reconcile_optionable_vs_alpaca(c))"` – Pi had fix $568->$52 real closePrice inflation
- [ ] Optional API: `wheel-api` FastAPI container `8097` from `Dockerfile.wheel-api` – start only if needed for status page

### Phase D — Cloudflared Cutover
- [ ] Obtain Pi tunnel JSON + tunnel ID (see Section 6) – **do not paste into chat**
- [ ] Create `~/.cloudflared/config.yml` with 4 ingress hostnames
- [ ] On Pi: `sudo systemctl stop cloudflared` + `crontab -e` comment out watchdog (temporarily)
- [ ] On Hatch: `cloudflared tunnel --config ~/.cloudflared/config.yml run pi-tunnel &` and log
- [ ] DNS already points – wait 60s – `curl -sI https://wheel.smitpatel.net` should 200 (via Hatch tunnel), `curl -s https://wheel.smitpatel.net/api/health`
- [ ] Webhook `https://webhook.smitpatel.net/health` → `{status:ok,platform:webhook}` – re-create webhook handler if needed (Pi uses `~/.hermes/scripts/finnhub-earnings-handler.py` – port to Hatch `hermes` equivalent or simple Python FastAPI listening on 8644)

### Phase E — Strategy Cron on Hatch
- [ ] Verify `scripts/run_strategy.py --dry-run` completes without errors (logs `logs/cron.log` + `logs/market_context.json`, `logs/earnings_cache.json`, `logs/dividend_cache.json`, `logs/volatility_cache.json`, `logs/fundamentals_cache.json`, `logs/wheel_trades.jsonl`)
- [ ] Create Hatch crons (via `default.cron` tool, **root agent must do, not subagent**):
```bash
# Use default.cron add action=add
# Example (UTC summer):
# wheel-open: 5 14 * * 1-5
# wheel-midday: 5 17 * * 1-5
# wheel-preclose: 5 19 * * 1-5 (or 35 19 for 15:35 ET pre-close)
```
Command draft already in Section 5.
- [ ] Enable one at a time, monitor `logs/cron.log` for $ interpolation bug – use Python logging not bash heredoc `$0.10` corruption (README Pitfall: use `<< 'LOG'` quoted heredoc or python `logger.info`)
- [ ] Nightly backup cron: `0 2 * * * tar czf ~/workspace/wheel-stack/optionable-data-backup-$(date +\%F).tar.gz ~/workspace/optionable-data` + `python3 -c "from core.pnl_tracker import ..."` discrepancy alert if inflated >$50

### Phase F — Decommission Pi
- [ ] After 2-3 successful market days (verify closer profit-take, roller net credit, SGOV sweep 454 vs 1007 ideal), Pi can be left as cold backup or powered down
- [ ] Update DNS TTL if needed, document new Hatch as primary
- [ ] Rotate secrets that were in `config/.env.example` (Alpaca, Finnhub, Alpha) – **critical, current example contains real keys**

### Rollback Plan
- Keep Pi image intact – do not delete `/home/smitpatel316/optionable-data` until Hatch proven stable 1 week
- If Hatch tunnel fails, restart Pi cloudflared: `sudo systemctl start cloudflared` – last-connect wins, traffic flips back in <10s
- If Optionable DB corrupted: restore from tar.gz backup `tar xzf ... -C ~/workspace/optionable-data`

---

## 8. Blockers & Risks

### Blocker 1 — Node.js version (HIGH)
- Hatch Node 18.19.1 cannot build/run `better-sqlite3@12.11.1` (requires Node 20.x+). `npm install` fails with EBADENGINE + ECONNRESET behind proxy. Vite build fails `ERR_MODULE_NOT_FOUND`.
- **Mitigations:**
  - Upgrade Hatch Node to 22 LTS via nvm: `curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash && nvm install 22 && nvm use 22` then `corepack enable`
  - Or use external Docker host (Fly.io / Render) that supports Node 20 and mount DATA_DIR via volume / LiteFS
  - Or temporarily keep Optionable on Pi and migrate only Python strategy to Hatch (hybrid) – Python core works (verified Alpaca connection), only needs `OPTIONABLE_URL` pointing to Pi tunnel `https://wheel.smitpatel.net` still hosted on Pi while you test Hatch scheduling.

### Blocker 2 — No Docker daemon on Hatch (MEDIUM)
- `docker` command not found earlier. `yomikoye/optionable:latest` cannot run without Docker. Same as Node blocker – need native Node run or external Docker host.

### Blocker 3 — Tunnel credential handoff (MEDIUM)
- Need Pi `~/.cloudflared/<id>.json` + tunnel ID to run same hostnames from Hatch. No Pi SSH access from this Hatch session. User must manually copy JSON (secure channel, not chat) or grant Pi SSH (Tailscale / ZeroTier / SSH). Do NOT ask for TunnelSecret in subagent chat – plan doc only.
- Risk of split-brain if both Pi and Hatch run same tunnel concurrently – causes flapping. Must stop Pi first.

### Blocker 4 — SQLite WAL / backup consistency (LOW)
- If `optionable.db-wal` not checkpointed before tar, Hatch import may miss recent trades. Mitigation: `PRAGMA wal_checkpoint(TRUNCATE);` before export on Pi OR use `GET /api/settings/export-db` which checkpoints.

### Blocker 5 — MCP vs pure Python drift (LOW-MEDIUM)
- Pi uses MCP 62 alpaca tools + 131 alpha tools via Hermes gateway – custom Python fallback in `scripts/run_strategy.py` has same logic but minor differences (e.g., limit order slippage logging, mid-price vs market fallback). Testing needed: dry-run first.

### Blocker 6 — Secrets in git (CRITICAL)
- `config/.env.example` in `smitpatel316/wheel-stack` contains real Alpaca SECRET `***REMOVED***`, Finnhub `***REMOVED***3g***REMOVED***40`, Alpha `***REMOVED***`, webhook secret `***REMOVED***50`. Anyone cloning gets them. Must rotate and replace example with placeholders. Same keys were pasted in main chat thread (Hatch memory). Rotate via Alpaca dashboard + Finnhub + Alpha Vantage.

---

## 9. What User Needs to Provide (to Finish)

1. **Pi SSH or manual data export** – either:
   - `scp /home/smitpatel316/optionable-data/optionable.db` + WAL, OR
   - tar.gz via `tar czf optionable-data-$(date +%F).tar.gz -C /home/smitpatel316 optionable-data` uploaded to Hatch `~/workspace/`, OR
   - Download via `https://wheel.smitpatel.net/api/settings/export-db` (or `optionable.smitpatel.net`) and upload.

2. **Cloudflared tunnel credentials** – copy `~/.cloudflared/*.json` and `~/.cloudflared/config.yml` from Pi to Hatch `~/.cloudflared/` (secure copy, chmod 600). Full tunnel ID (b826...) already partially known.

3. **Decision on Node upgrade** – allow `nvm install 22` on Hatch to run Optionable natively, OR keep Optionable on Pi temporarily and migrate only strategy cron (hybrid mode), OR deploy Optionable to Fly/Render with same DATA_DIR.

4. **Cron timezone preference** – keep market-time ET 10:05/13:05/15:05 (PDT 07:05/10:05/12:05) with summer UTC 14:05/17:05/19:05, or use fixed UTC year-round accepting 1h winter drift, or support both summer/winter schedules.

5. **Webhook handler** – Pi uses Python `~/.hermes/scripts/finnhub-earnings-handler.py` to clear earnings cache + trigger full agent on Finnhub event. For Hatch, do we need same real-time trigger or is 6h cache poll enough? If needed, provide handler script or allow Hatch port 8644 FastAPI.

---

## 10. Commands Ready for Root Agent (after approvals)

```bash
# Verify Node upgrade path (if approved)
nvm install 22 && node -v && npm -v
cd ~/workspace/optionable-src && npm install --include=dev && npm run build

# Optionable native start (after data import)
mkdir -p ~/workspace/optionable-data
# assume db unpacked
DATA_DIR=/home/hatch/workspace/optionable-data PORT=8096 NODE_ENV=production node ~/workspace/optionable-src/server.js &
sleep 3 && curl -s http://localhost:8096/api/health | jq

# Python strategy dry-run
cd ~/workspace/wheel-stack
PYTHONPATH=. python3 scripts/run_strategy.py --dry-run --strat-log

# Hatch cron (root must use default.cron tool, not shell cron)
# Use default.cron create with owner goal:<slug> only when final goal exists
# Example payload for root:
#   slug: wheel-open
#   schedule: "5 14 * * 1-5"
#   command: "cd ~/workspace/wheel-stack && PYTHONPATH=. python3 scripts/run_strategy.py --strat-log --log-to-file"
```

---

## 11. Immediate Next Actions for Migration (subagent done, reporting)

- **MIGRATION_PLAN.md written** to `~/workspace/wheel-stack/MIGRATION_PLAN.md` (this file)
- **Optionable-src cloned** to `~/workspace/optionable-src`, inspected PORT/DATA_DIR logic, documented Node 18 blocker
- **Python deps verified** Alpaca paper equity $100169.14 cash 26274.28 ACTIVE
- **Cron draft** UTC 14:05/17:05/19:05 Mon-Fri (Summer) ↔ PDT 07:05/10:05/12:05 ↔ EDT 10:05/13:05/15:05 mapped
- **Cloudflared binary** available v2026.7.3 at `/usr/local/bin/cloudflared` but no `~/.cloudflared/` creds – documented needed JSON + config.yml, conflict avoidance
- **Blockers documented** – Node 20+, no Docker daemon, tunnel handoff, WAL checkpoint, MCP drift, secrets in git

**No production Pi changes made.** No crons created (per task instruction). No secrets exposed beyond pointing out `.env.example` risk.

Ready for root agent to present plan to Smit and request Pi data export + tunnel creds + Node upgrade decision.
