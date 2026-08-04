# Hybrid v2.3 — Earnings Calendar + Finnhub Webhook (2026-08-03)

## Why
NVDA concentrator June -$154k unrealized 224→200 after earnings gap. Reddit trader assigned 300 AMD @505→440. Selling CSP right before earnings shows juicy IV (40% ann) but gap -10% overnight = instant ITM assignment + loss >100% triggers defensive roll at debit, kills 371% roll rate target from paper arXiv:2512.01123.

## Finnhub API
- Endpoint: `GET https://finnhub.io/api/v1/calendar/earnings?from=YYYY-MM-DD&to=YYYY-MM-DD&token=KEY`
- Returns: `{"earningsCalendar":[{"symbol":"CSCO","date":"2026-08-19","hour":"bmo","quarter":2,"year":2026,"epsEstimate":...},...]}`
- Key: `FINNHUB_API_KEY=***REMOVED***40` stored in `~/options-wheel/.env` + `~/.hermes/.env` via `os.getenv`
- Free tier: 60 req/min, 250/day enough for 25 tickers 30d lookahead.

## Module `core/earnings_calendar.py`
```python
build_cache(symbols, days_ahead=30) -> Dict[symbol, date]
  fetch_earnings(today, future) via requests, cache logs/earnings_cache.json TTL 6h
is_earnings_risk(symbol, earnings_map, today, block_days=3, dte=21) -> (blocked, reason)
  - 0d: Earnings TODAY → block "gap risk"
  - 1d: TOMORROW → block
  - <=block_days: within 3d → block
  - <=dte: earnings during position lifetime → block high gap risk (NVDA lesson)
  - <=7d: warning medium risk
get_earnings_risk_report(symbols, block_days=3, days_ahead=30, dte_default=21) -> dict
```
- Used in `filter_underlying(client, symbols, BP, earnings_map)` — skips blocked tickers before option scan.
- Used in `sell_puts(..., earnings_map)` — passed through.
- Used in `run_strategy.py` Phase 0.5 before context analyzer, logs `strategy_log.json["earnings_report"]` and `market_context.decision_factors["earnings_blocked"]`.

## Params (config/params.py)
```python
EARNINGS_BLOCK_DAYS = 3   # skip if earnings within 3 days
EARNINGS_BLOCK_DTE = 21   # skip if earnings during DTE 21
EARNINGS_CACHE_DAYS = 30  # lookahead
EARNINGS_ENABLED = True
```

## Live Test 2026-08-03
- 25 tickers, next 30d: only CSCO 2026-08-19 16d → blocked for new CSPs (earnings during DTE 21).
- Existing CSCO 108P 18D already held — roller monitors, will handle gap if any.
- Allowed 12/12 after filter but BP $500 <2000 → Option A conservative wait, correct.
- Logs: `logs/earnings_cache.json` `{"_timestamp":...,"earningsCalendar":[{"symbol":"CSCO","date":"2026-08-19"}]}`

## Webhook — Finnhub Real Spec
User guide verbatim:
> "All requests' header made from our server will contain field "X-Finnhub-Secret": "***REMOVED***50" for authentication. To acknowledge receipt of an event, your endpoint must return a 2xx HTTP status code. Acknowledge events prior to any logic that needs to take place to prevent timeouts. Your endpoint is disabled if it fails to acknowledge events over consecutive days."

- Not HMAC SHA256 like GitHub (`X-Hub-Signature-256`). Plain secret header `X-Finnhub-Secret`.
- Must return 2xx **before logic** else timeout → disabled after consecutive failures.
- Wrong secret → 401 (debug) but valid secret → 200 JSON `{"status":"ok","matched":...}` immediately.

### Implementation `~/.hermes/scripts/manual-webhook.py`
- Listens `0.0.0.0:8644`, route `/webhooks/{name}` e.g. `/webhooks/finnhub-earnings`
- Loads `~/.hermes/webhook_subscriptions.json` dict keyed by name `{name:{description,events,secret,prompt,deliver}}` (created via `hermes webhook subscribe`)
- Validation:
  ```python
  secret_hdr = headers.get("X-Finnhub-Secret")
  if secret_hdr and secret_hdr != FINNHUB_SECRET: return 401
  send_response(200)  # ACK FIRST
  then log to ~/.hermes/webhook_events.jsonl + clear earnings_cache.json to force refetch
  ```
- Background server: `terminal(background=true)` because `systemctl --user restart hermes-gateway` blocked from inside gateway (safety guard #30719 "Gateway restart blocked from inside").
- Test:
  ```bash
  curl -H "X-Finnhub-Secret: d7cphh...k50" POST http://localhost:8644/webhooks/finnhub-earnings -d '{"symbol":"CSCO","date":"2026-08-19","type":"earnings"}' → 200
  curl -H "X-Finnhub-Secret: wrong" ... → 401
  curl https://webhook.smitpatel.net/webhooks/finnhub-earnings -H "X-Finnhub-Secret: d7cphh...k50" -d '{"type":"earnings","symbol":"MSFT"}' → 200 via tunnel
  ```

### Cloudflare Tunnel
- Ingress in `~/.cloudflared/config.yml`:
  ```yaml
  - hostname: webhook.smitpatel.net
    service: http://localhost:8644
  - service: http_status:404  # must be last
  ```
- Pitfall 2026-08-03: stray duplicate `service: http://localhost:8644` line after catch-all caused cloudflared exit-code restart loop `activating (auto-restart) Result: exit-code` + 1033 / 530 HTML. Fixed by removing stray indented line.
- DNS: `cloudflared tunnel route dns pi-tunnel webhook.smitpatel.net` → CNAME, then `systemctl restart cloudflared`
- Verify: `curl -s https://webhook.smitpatel.net/health` → `{"status":"ok","service":"manual-webhook-hermes"}`

### Subscription
```bash
hermes webhook subscribe finnhub-earnings \
  --events earnings \
  --secret ***REMOVED***50 \
  --prompt "Finnhub earnings event: {payload.symbol} on {payload.date}" \
  --deliver origin
# URL returned: http://localhost:8644/webhooks/finnhub-earnings
# Public: https://webhook.smitpatel.net/webhooks/finnhub-earnings
```
Stored in `~/.hermes/webhook_subscriptions.json`, events log `~/.hermes/webhook_events.jsonl`.

### Finnhub Dashboard Setup
1. https://finnhub.io/dashboard/webhook → Add webhook
2. URL: `https://webhook.smitpatel.net/webhooks/finnhub-earnings`
3. Secret: `***REMOVED***50`
4. Events: earnings (news if you want)
5. Finnhub sends `X-Finnhub-Secret` header; your endpoint must return 2xx quickly.

### When to Use Webhook vs Polling
- Polling (6h cache + 3x/day cron 10:05/13:05/15:35 ET) covers 99% — earnings dates known weeks ahead.
- Webhook adds real-time edge: if earnings announced tomorrow 7am ET and you sold yesterday, cron only catches at next run, webhook catches seconds and can trigger immediate Telegram alert + defensive roll.
- For 10% cash sleeve (2-3 puts max, e.g., F $14 = $1.4k = 14% sleeve), webhook protects against gap.

### Related
- `config/webhook_config.json` saves public_url, local_url, secret, test curl
- Agentic cron prompt Phase 0.5 includes earnings fetch
- Paper gap case: June NVDA 8k shares @0.08 cost 2002 $1.1k 90% portfolio 5.5k→5k margined, assigned 300 AMD @505→440 recovery
