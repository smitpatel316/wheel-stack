# Deployment — Pi Budupi Wheel Stack

Paper-only $100k hybrid v2.4-v2.5.3 Model-First agentic.

## Prerequisites

- Pi hostnames: `budupi` Linux arm64, user `smitpatel316` home `/home/smitpatel316`
- Docker + compose v2: `sg docker` group `docker` no sudo needed
- Hermes Agent: `~/.hermes/` config.yaml, cron jobs.json, skills/
- Cloudflared tunnel: `pi-tunnel` id `b826eba9-...` credentials `~/.cloudflared/`
- Domain: `wheel.smitpatel.net` -> 8096 Optionable, `webhook.smitpatel.net` -> 8644 Finnhub webhook, `optionable.smitpatel.net` alias

## Env Setup

```bash
cd ~/wheel-stack || mkdir -p ~/wheel-stack && cd ~/wheel-stack

# .env from legacy options-wheel or create
if [[ -f ~/options-wheel/.env ]]; then
  cp ~/options-wheel/.env .env
else
  cat > .env <<'ENV'
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
IS_PAPER=true
FINNHUB_API_KEY=***REMOVED***50
ALPACA_API_KEY_PAPER=...
ALPACA_SECRET_PAPER=...
FINNHUB_API_KEY=***REMOVED***3g***REMOVED***40
ALPHA_VANTAGE_API_KEY=***REMOVED***
OPENAI_API_KEY=...
OPTIONABLE_URL=http://localhost:8096
TZ=America/Los_Angeles
ENV
fi

# Also set in Hermes env
cat ~/.hermes/.env 2>/dev/null | grep ALPACA || echo "Set ALPACA keys in ~/.hermes/.env too"
```

Required keys:
- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` paper account PA3WFOAHE2C6 level3 4x buying power 350k options 75k
- `ALPHA_VANTAGE_API_KEY=***REMOVED***` for earnings+dividend+fundamentals+volatility
- `FINNHUB_API_KEY=***REMOVED***40` or `***REMOVED***50` webhook secret variant (earnings + webhook secret same prefix)
- `IS_PAPER=true` never flip without explicit permission

## Docker Deployment

### Unified docker-compose.yml (root)

Contains:
- `optionable` service `yomikoye/optionable:latest` 8096:8080 volume `optionable-data:/data` + legacy `/home/smitpatel316/optionable-data` compat fallback, network `wheel-net`, healthcheck `/api/health`
- Optional `wheel-runner` profile `manual` build Dockerfile.wheel for dry-run testing only, production uses Hermes cron not this runner

```bash
cd ~/wheel-stack

# Ensure volume data dir
mkdir -p /home/smitpatel316/optionable-data || mkdir -p ./optionable-data
sudo chown -R 1000:1000 /home/smitpatel316/optionable-data 2>/dev/null || true

# Compose up
sg docker -c "docker compose up -d --remove-orphans"
# or docker compose up -d

# Wait health 30s
for i in {1..20}; do curl -sf http://localhost:8096/api/health && break; sleep 2; done
curl -s http://localhost:8096/api/health | python3 -m json.tool
# Expected: {status:"ok", version:"0.16.0", ...}

# Logs
sg docker -c "docker logs optionable --tail 50"
sg docker -c "docker ps --format '{{.Names}} {{.Ports}} {{.Status}}'"
```

### Optionable Original Compose (legacy)

`~/wheel-stack/optionable/docker-compose.yml` just optionable service:

```yaml
services:
  optionable:
    container_name: optionable
    image: yomikoye/optionable:latest
    ports: ['8096:8080']
    environment: [TZ=America/Los_Angeles, NODE_ENV=production, DATA_DIR=/data]
    volumes: [/home/smitpatel316/optionable-data:/data]
    restart: unless-stopped
```

Unified root `docker-compose.yml` improves with:
- Named volume `optionable-data` + healthcheck + labels + wheel-net network + optional wheel-runner manual profile
- Use root for deployment: `pi/deploy.sh` does `sg docker compose up -d`

### Verify Docker

```bash
curl -s http://localhost:8096/api/health | jq
curl -s http://localhost:8096/api/trades | jq '.data | length' # tradeCount 15
curl -s http://localhost:8096/api/stocks | jq
curl -s http://localhost:8096/api/fund-transactions | jq
```

## Hermes Cron Deployment

### Install agentic cron

From `~/wheel-stack/hermes/cron/`:

```bash
cd ~/wheel-stack

# List existing
hermes cronjob list
cat ~/.hermes/cron/jobs.json | python3 -c "import json; print(json.dumps([{'name':j['name'],'sched':j['schedule_display'],'enabled':j['enabled']} for j in json.load(open('/home/smitpatel316/.hermes/cron/jobs.json'))['jobs']], indent=2))"

# Delete old if exists (id 014708b33a6a)
hermes cronjob delete options-wheel-agentic || true

# Create from prompt.md (6.5k hybrid Model-First Phases 0.1-6)
hermes cronjob create \
  --schedule "5 7,10,12 * * 1-5" \
  --name options-wheel-agentic \
  --skills options-wheel-trading,alpaca-mcp \
  --prompt "$(cat hermes/cron/options-wheel-agentic.prompt.md)"

# Show
hermes cronjob show options-wheel-agentic
```

Schedule `5 7,10,12 * * 1-5` = ET 10:05/13:05/15:35 Mon-Fri per spec, UTC underlying.

### Hermes MCP

```bash
hermes mcp list
# Expected: alpaca ✓ enabled 62 tools, alphavantage 131 if configured

# Gateway restart guard #30719: blocked from inside gateway process (Telegram agent) exit -1 anti-loop
# MUST restart via SSH outside, NOT via terminal() tool:
systemctl --user restart hermes-gateway.service  # from SSH shell

# After restart check watchdog
ps aux | grep mcp_stdio_watchdog | grep alpaca
# mcp_stdio_watchdog.py --ppid <gateway> -- uvx alpaca-mcp-server

# Tool search verification from agent
tool_search mcp__alpaca__get_account_info  # should find 62
tool_search mcp__alphavantage__EARNINGS_CALENDAR # 131
```

### Test Cron Run (paper dry)

See `hermes/cron/README.md` for dry run commands evaluating closer/roller without placing orders until verified.

Real trigger:

```bash
hermes cronjob run options-wheel-agentic

# Tail logs
tail -100 ~/wheel-stack/logs/cron.log || tail -100 ~/options-wheel/logs/cron.log
hermes cronjob logs options-wheel-agentic --tail 100
```

## Cloudflare Tunnel Two-Step

**Mandatory 2 steps else ERR_NAME_NOT_RESOLVED**:

1. **Ingress** in `~/.cloudflared/config.yml` — specific hostnames FIRST, catch-all last:

```yaml
tunnel: pi-tunnel
credentials-file: /home/smitpatel316/.cloudflared/b826eba9-xxxx.json
ingress:
  - hostname: wheel.smitpatel.net
    service: http://localhost:8096
  - hostname: optionable.smitpatel.net
    service: http://localhost:8096
  - hostname: webhook.smitpatel.net
    service: http://localhost:8644
  - service: http_status:404
```

Merge snippet from `pi/cloudflared-config-snippet.yml`.

2. **DNS CNAME**:

```bash
cloudflared tunnel route dns pi-tunnel wheel.smitpatel.net
cloudflared tunnel route dns pi-tunnel optionable.smitpatel.net
cloudflared tunnel route dns pi-tunnel webhook.smitpatel.net
```

If CNAME already exists, it will error "already routed" — ok.

3. **Restart** and wait 60s:

```bash
sudo systemctl restart cloudflared || systemctl --user restart cloudflared
# or sg docker restart cloudflared if using docker
sleep 60
```

Pitfall 2026-08-03: stray duplicate `service: http://localhost:8644` line without hostname caused cloudflared 1033 / exit-code restart loop exit-code. Fixed by removing stray, ensuring only one list entry per hostname, catch-all last.

### Verification Curls

```bash
# Local direct
curl -s http://localhost:8096/api/health | jq
# {
#   "status": "ok",
#   "version": "0.16.0"
# }

curl -s http://localhost:8096/api/trades | jq '{count: (.data|length), sample: .data[0]}'

# Via cloudflared ingress (after 60s DNS)
curl -s https://wheel.smitpatel.net/api/health | jq
curl -s https://optionable.smitpatel.net/api/health | jq
# Should be same as local

# Webhook health
curl -s https://webhook.smitpatel.net/health | jq
# Expected {status:ok platform:webhook}

# Webhook earnings endpoint test plain header auth
curl -i -H "X-Finnhub-Secret: ***REMOVED***50" -X POST https://webhook.smitpatel.net/webhooks/finnhub-earnings -d '{"data":[{"symbol":"AAPL","date":"2020-03-03","eps_actual":17.5}],"event":"earnings"}' -H "Content-Type: application/json"
# Expected 200 {"status":"ok","matched":"finnhub-earnings"}
# Wrong secret -> 401
# Logs in ~/.hermes/webhook_events.jsonl + clears earnings_cache to force refetch + triggers wheel agent via options-wheel-trading skill

# System status
sg docker -c "docker ps --format '{{.Names}} {{.Ports}} {{.Status}}' | grep -E 'optionable|cloudflared'"
systemctl --user status cloudflared --no-pager || sudo systemctl status cloudflared --no-pager | tail -30
crontab -l  # should be 2 jobs only cloudflared watchdog + backup 2am
hermes cronjob list
```

### Pi Deploy Script

`pi/deploy.sh` unified:

```bash
chmod +x ~/wheel-stack/pi/deploy.sh
~/wheel-stack/pi/deploy.sh
# Does: env check, sg docker compose up -d, health wait, cloudflared config check, hermes cron list, mcp list, backup instructions, verification curls
```

## Backup

- System cron 2am: `cp ~/optionable-data/optionable.db backup` — handles sqlite3 missing fallback
```bash
# Check crontab
crontab -l | grep backup

# Manual backup
cp /home/smitpatel316/optionable-data/optionable.db ~/optionable-backup-$(date +%F).db
# or docker
docker cp optionable:/data/optionable.db ~/optionable-backup-$(date +%F).db
# or volume
sg docker -c "docker run --rm -v optionable-data:/data -v $HOME:/backup alpine cp /data/optionable.db /backup/optionable-$(date +%F).db"

# Hermes logs also backup
tar czf ~/wheel-stack-backup-$(date +%F).tgz ~/wheel-stack/logs ~/.hermes/cron/jobs.json ~/optionable-data/
```

## Safety & Post-Deploy Checks

- `IS_PAPER=true` never false without explicit permission
- `hermes cronjob list` last_status ok last_run_at recent
- `curl http://localhost:8096/api/health` ok version v0.16.0 tradeCount matches positions count + closed
- `get_account_info` equity ~$99k cash $55k after SGOV sweep P/L -$200ish day1 spread decay normal
- SGOV qty 688 $69k MV real affordable vs ideal 1007 $101k $440/mo diff logged due to stockBP limit
- Closer 0-1 per run max 3 highest profit first buy_to_close, roller max 2 net credit $0.10 spread filter
- Optionable P/L bug check `SELECT COUNT(*) FROM trades WHERE status!='Open' AND closePrice=0` should be 0 after v2.5.4 fix else warns

## Rollback

```bash
# Docker
sg docker -c "docker compose -f ~/wheel-stack/docker-compose.yml down"
sg docker -c "docker compose -f ~/optionable-data/docker-compose.optionable.yml up -d" # legacy

# Hermes cron
hermes cronjob delete options-wheel-agentic
# restore from ~/.hermes/cron/jobs.json backup if needed

# Data
cp ~/optionable-backup-2026-08-04.db /home/smitpatel316/optionable-data/optionable.db
sg docker -c "docker restart optionable"
```

See also `../hermes/cron/README.md`, `../hermes/mcp/README.md`, `hermes/skill/SKILL.md`, `pi/cloudflared-config-snippet.yml`.
