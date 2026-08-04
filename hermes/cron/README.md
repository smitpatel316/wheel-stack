# Hermes Cron — Options Wheel Agentic

Paper wheel on Pi budupi $100k, hybrid v2.4-v2.5.3 Model-First.

## Install as Hermes Cron

From `~/wheel-stack`:

```bash
# Ensure skills exist in Hermes (options-wheel-trading, alpaca-mcp, alphavantage-mcp optional)
hermes skills list | grep options-wheel

# Create/update cron job from prompt.md
hermes cronjob create \
  --schedule "5 7,10,12 * * 1-5" \
  --name options-wheel-agentic \
  --skills options-wheel-trading,alpaca-mcp \
  --prompt "$(cat hermes/cron/options-wheel-agentic.prompt.md)"

# Or update existing (delete then create)
hermes cronjob delete options-wheel-agentic
hermes cronjob create --schedule "5 7,10,12 * * 1-5" --name options-wheel-agentic --skills options-wheel-trading,alpaca-mcp --prompt "$(cat hermes/cron/options-wheel-agentic.prompt.md)"

# List / status
hermes cronjob list
hermes cronjob show options-wheel-agentic
```

Schedule meaning: `5 7,10,12 * * 1-5` UTC = 03:05, 06:05, 08:05 ET? Actually job config shows ET 10:05/13:05/15:35 — 7:05 UTC = 3:05 ET, 10:05 UTC = 6:05 ET, 12:05 UTC = 8:05 ET? Legacy display is PDT `5 7,10,12 * * 1-5 ET 10:05/13:05/15:35` — Pi cron uses ET conversion logic. Keep exactly `5 7,10,12 * * 1-5`.

### Skills Required
- `options-wheel-trading` — contains full logic references, params, pitfalls, MCP execution patterns, SGOV sweep, webhook
- `alpaca-mcp` — 62 tools
- Optional `alphavantage` MCP is registered via gateway mcp config not cron skills

### How to Test (dry run, paper)

Local Python test without cron triggering orders:

```bash
cd ~/wheel-stack || cd ~/options-wheel
source .venv/bin/activate

# Phase 0: earnings/block check — no order placement
FINNHUB_API_KEY=... python -c "from core.earnings_calendar import get_earnings_risk_report; print(get_earnings_risk_report(['AAPL','CSCO'], block_days=3))"

# Phase 1: context analyzer
FINNHUB_API_KEY=... ALPHA_VANTAGE_API_KEY=... python -c "from core.context_analyzer import analyze_context; from core.broker_client import BrokerClient; from config.credentials import *; cli=BrokerClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, IS_PAPER); print(analyze_context(cli).to_dict())"

# Phase 2 & 3: closer + roller evaluate only (no execution)
python -c "from core.closer import evaluate_all_for_close; from core.broker_client import BrokerClient; from config.credentials import *; cli=BrokerClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, IS_PAPER); dec=evaluate_all_for_close(cli); print([(d.candidate.underlying, d.profit_pct, d.should_close) for d in dec])"

python -c "from core.roller import evaluate_all_positions; from core.broker_client import BrokerClient; from config.credentials import *; cli=BrokerClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, IS_PAPER); print(evaluate_all_positions(cli))"
```

Agentic dry run via Hermes:

```bash
hermes cronjob trigger options-wheel-agentic --dry-run  # if supported, else manual
# Or invoke agent directly with prompt subset
```

To trigger real agent now (paper only):

```bash
hermes cronjob run options-wheel-agentic
# check logs
tail -100 /home/smitpatel316/wheel-stack/logs/cron.log || tail -100 ~/options-wheel/logs/cron.log
hermes cronjob logs options-wheel-agentic --tail 50
```

### Verification After Cron Run

```bash
# Account
curl -s http://localhost:8096/api/health | jq
curl -s http://localhost:8096/api/trades | jq '.data | length'

# Hermes cron status
hermes cronjob list
cat ~/.hermes/cron/jobs.json | python3 -c "import json,sys; data=json.load(sys.stdin); j=next(x for x in data['jobs'] if x['name']=='options-wheel-agentic'); print(j['last_status'], j['last_run_at'], j['next_run_at'])"

# System crons should remain 2 only
crontab -l

# MCP liveness
hermes mcp list
ps aux | grep -i alpaca-mcp | grep -v grep
```

### Environment Variables Required
In `~/wheel-stack/.env` or `~/options-wheel/.env` and `~/.hermes/.env`:

```
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
IS_PAPER=true
FINNHUB_API_KEY=***REMOVED***...
ALPHA_VANTAGE_API_KEY=***REMOVED***
OPENAI_API_KEY=... # optional LLM enrichment
OPTIONABLE_URL=http://localhost:8096
```

### Common Pitfalls
- Gateway restart blocked from inside agent (safety #30719). Must `systemctl --user restart hermes-gateway` via SSH outside.
- Cron log `$` interpolation corruption: always use `<< 'LOG'` quoted heredoc or python file write when logging strings containing `$0.10`.
- `filter_underlying` needs `build_cache()` raw Dict[str,date], not `get_earnings_risk_report()` Dict-of-Dicts.
- `sync_sgov_to_optionable(client)` single arg.

See also `../skill/SKILL.md` and references.
