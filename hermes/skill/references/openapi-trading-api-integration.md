# Full OpenAPI Integration — trading-api.json 203KB v2.0.1 (2026-08-03)

**Spec:** `https://docs.alpaca.markets/us/openapi/trading-api.json` — 40 paths, 134 schemas, downloaded to `/tmp/trading-api.json`.

## Paths Audited

```
/v2/account, /v2/account/activities + /{activity_type}, /v2/account/configurations, /v2/account/portfolio/history
/v2/orders, /v2/orders/{order_id}, /v2/positions, /v2/positions/{id}/exercise, /v2/positions/{id}/do-not-exercise
/v2/watchlists, /v2/clock, /v2/calendar, /v2/assets, /v2/options/contracts
/v1/locates, /v2beta1/events/activities (SSE - Broker only, 404 for Trading API)
```

### ActivityType Enum (components/schemas/ActivityType)
```
FILL, TRANS, MISC, ACATC, ACATS, CSD, CSW, DIV, DIVCGL, DIVCGS, DIVNRA, DIVROC,
FEE, INT, INTNRA, JNL, JNLC, JNLS, MA, NC, OPASN (assignment), OPCA, OPEXP (expiration),
OPEXC (exercise), OPTRD (trade), PTC, REORG, SPIN, SPLIT, FOPT, OCT
```

## Implementations Based on Spec

### 1. Watchlist Sync — wheel-universe 25 symbols
```python
# POST /v2/watchlists
headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
symbols = [line.strip() for line in open('config/symbol_list.txt')]
requests.post(f"{base}/v2/watchlists", headers=headers, json={"name":"wheel-universe","symbols":symbols})
# PUT if exists: /v2/watchlists/{id} with same payload
```
Live: id `40cc59d4-d212-4c34-8849-13bb92c0ecee` account `1bb85c4e-...` 25 assets.
File: `scripts/sync_watchlist.py`

### 2. Clock Guard — Market Hours Check
```
GET /v2/clock -> {is_open: bool, next_open, next_close, timestamp}
```
`scripts/is_market_open.py`: REST call, prints `is_open`, exit 0 open else 1.
Cron wrapper `run_wheel_cron.sh`:
```bash
python3 scripts/is_market_open.py > /tmp/clock.out; CLOCK_EXIT=$?
if [ $CLOCK_EXIT -ne 0 ]; then
  echo "Market closed - skipping wheel, but SGOV sync still"
  python3 scripts/sync_sgov.py dynamic
  python3 -c "sync_alpaca_equity+sgov+closed+dividends"
  exit 0
fi
# ... proceed with run-strategy
```
Verified Sunday `is_open False next_open 2026-08-03T09:30:00-04:00` exit 1 -> closed path still syncs SGOV 496.

### 3. Activities Sync — Dividends, Fees, Assignments
SDK 0.43.5 pitfall: `GetAccountActivitiesRequest` missing, `TradingClient.get_account_activities` missing.
Workaround: raw REST

```python
base = "https://paper-api.alpaca.markets" if IS_PAPER else "https://api.alpaca.markets"
headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
for atype in ["DIV","INT","FEE","OPASN","OPEXP","OPEXC","JNLS","TRANS"]:
    r = requests.get(f"{base}/v2/account/activities/{atype}", headers=headers, params={"page_size":50, "after": "2026-01-01"})
    # atype DIV returns [] when no dividends yet (paper fresh) -> 200 []
    # Map: DIV* -> dividend, INT* -> interest, FEE -> fee via POST /api/fund-transactions
    # OPASN -> Assigned when stock appears, OPEXP -> Expired
```

`core/activities_sync.py`:
- `fetch_activities(client, type)` raw REST
- `sync_dividends_and_interest()`: GET DIV variants + INT + FEE, dedupe via (amount,date,desc[:30]) set vs existing `GET /api/fund-transactions`, POST new
- `sync_option_events()`: GET OPASN/OPEXP/OPEXC + call `sync_closed_trades()`
- `full_sync()` called from `run_strategy` after equity/sgov and from periodic task

Current paper: DIV 0, INT 0, OPASN 0 (fresh account) -> `200 []` correct, will auto-sync when SGOV monthly div ~$0.12 per share * 496 = ~$59.

### 4. Portfolio History & Closed Orders
- `GET /v2/account/portfolio/history?period=1A&timeframe=1D` -> timestamps[] equity[] for RoR verification, 76 days returned
- `GET /v2/orders?status=closed&limit=100` -> backfill: account shows only 1 canceled SGOV duplicate, no prior wheel trades (fresh)
- Account config: `GET /v2/account/configurations` -> fractional_trading true, max_margin 4, trade_confirm all; `GET /v2/account` -> options_approved_level 3, options_trading_level 3, options_buying_power 75k matches MAX_RISK

### 5. SSE Activities — Broker-Only 404
Spec lists `/v2beta1/events/activities` SSE `Subscribe to Activity Events`. Tested:
```
curl -H "APCA-API-KEY-ID: ..." https://paper-api.alpaca.markets/v2beta1/events/activities?since=2026-08-01
-> 404 {"message":"Not Found"}
```
Conclusion: Broker API only. Fallback to 30min periodic poll inside `alpaca_stream_sync.py` + cron poll (see `periodic_activities()` asyncio task sleep 1800).

### 6. Real-Time Stack Final (from spec + streaming docs)
- **TradingStream** `wss://paper-api.alpaca.markets/stream` trade_updates (new/fill/partial_fill/canceled/expired) -> systemd `alpaca-stream.service` active PID 2628773, connected, subscribed, logs `stream.log`
- **Activities Poll** raw REST DIV/INT/OPASN OPEXP every 30min inside stream service + cron extra block
- **Cron** ET 10:05/13:05/15:35 with clock guard
- **Backup** 2am sqlite3 .backup + gzip 30 retain

### 7. Exercise / DNE Edge
`/v2/positions/{id}/exercise` POST and `/do-not-exercise` POST. Alpaca default ITM auto-exercise at expiry. For wheel CSP, you want assignment (stock) not exercise of long option. Long calls/puts not used in wheel (only short), so DNE not needed currently. If long put hedge added later, need DNE logic.

## Verification After Integration
```
Watchlists: wheel-universe 25 -> 200 created
Clock: is_open False Sunday exit 1 -> closed path runs SGOV sync still
Activities: DIV 0 INT 0 OPASN 0 -> 200 [] API works
Fund 1 deposit 100k, stocks SGOV 496, trades 0 Sunday
Stream: Active running since 21:33:58, connected to BaseURL.TRADING_STREAM_PAPER
Optionable Up 53min healthy
```

## Alpaca MCP Server Integration (2026-08-03) — Official v2.2.0 FastMCP from OpenAPI

**Source:** https://github.com/alpacahq/alpaca-mcp-server 898★ 265 forks official.
Completes rewrite v2: FastMCP 3.4.5 `FastMCP.from_openapi(spec, client, mcp_names=TOOL_NAMES)` loads `specs/trading-api.json` + `market-data-api.json` at process init. Hand-crafted overrides `register_order_tools` for place_stock/crypto/option order. TrustBoundaryMiddleware for prompt injection. Toolset filtering via ALPACA_TOOLSETS env.

**Install on Pi budupi:**
- Prereqs: `uv` (uvx), `pip install mcp` in Hermes venv `~/.hermes/hermes-agent/venv` (not options-wheel venv)
- Config `~/.hermes/config.yaml`:
```yaml
mcp_servers:
  alpaca:
    command: "uvx"
    args: ["alpaca-mcp-server"]
    env:
      ALPACA_API_KEY: "PKFOW... paper"
      ALPACA_SECRET_KEY: "..."
      ALPACA_PAPER_TRADE: "true"
      ALPACA_TOOLSETS: "account,trading,watchlists,assets,stock-data,options-data,fixed-income-data,corporate-actions"
    timeout: 60
    connect_timeout: 90
```
- Gateway restart required to load (blocked from inside gateway process: `hermes gateway restart` from SSH outside). Until restart, tools available via direct stdio_client test.
- Verified: 33 tools with account,trading,watchlists filter, 41 with full toolsets via `session.list_tools()`.

**Tools discovered (41):**
`place_stock_order, place_crypto_order, place_option_order, search_alpaca_docs, fetch_alpaca_doc, search_alpaca_api_specs, list_alpaca_api_endpoints, get_alpaca_endpoint_docs, get_account_info, get_account_activities, get_account_activities_by_type, get_account_config, update_account_config, get_portfolio_history, get_all_assets, get_asset, get_calendar, get_clock, get_corporate_action_announcements, get_corporate_action_announcement, get_option_contracts, etc` + watchlist + stock-data + options-data when enabled.

**Live test via StdioServerParameters:**
- `get_account_info` -> equity 100k buying_power 350045 options_buying_power 75022 options_approved_level 3 multiplier 4 account PA3WFOAHE2C6 ACTIVE (matches our broker_client)
- `get_clock` -> Unknown tool when toolset filter without assets, with assets true returns clock
- `get_account_activities_by_type DIV` -> {"result": []} 200 empty fresh paper correct

**Architecture final after MCP:**
- Real-time: TradingStream websocket trade_updates (fills within seconds) -> alpaca-stream.service systemd active PID 2628773 logs stream.log connected, periodic_activities() every 1800s for DIV/INT/OPASN
- REST: MCP mcp_alpaca_* tools available in Hermes chats after gateway restart for ad-hoc "What's my buying power?" queries, plus our legacy broker_client.py + activities_sync.py as fallback
- Cron: ET 10:05/13:05/15:35 with clock guard is_market_open.py, SGOV sync even when closed, Optionable healthcheck, backup 2am
- Tracker: Optionable v0.16.0 on 8096 wheel.smitpatel.net 2-step tunnel + DNS CNAME mandatory, fund 1 deposit, SGOV 496, trades 0 ready Monday
- Docs integration: MCP also includes search_alpaca_docs (Readme) + get_alpaca_endpoint_docs (OpenAPI docs) for LLM to self-lookup spec

**When to use MCP vs custom:**
- MCP good for: ad-hoc account checks from Telegram, natural language trading ("sell CSP..."), docs search, portfolio history queries inside chat
- Custom keeps: real-time stream (MCP is REST only, no websocket), SGOV idle calculation logic, Optionable idempotent POST with DELETE-before-POST pattern
- For Claude Desktop/Cursor: add same mcp_servers entry to claude_desktop_config.json / .cursor/mcp.json for natural language trading from IDE

See references/mcp-alpaca-integration.md for detailed MCP setup.

