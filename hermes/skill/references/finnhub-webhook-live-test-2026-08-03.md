# Finnhub Webhook Live Test — 2026-08-03

User message: "sent" after adding webhook URL in Finnhub dashboard.

## What was implemented
- **Manual webhook server** `~/.hermes/scripts/manual-webhook.py` because native Hermes webhook adapter only supports HMAC `X-Hub-Signature-256` (GitHub style), but Finnhub spec uses plain header `X-Finnhub-Secret: <secret>`
- **Spec per user-copied guide:** All requests header will contain field `X-Finnhub-Secret: <value>` for auth. Endpoint must return 2xx prior to any logic to prevent timeouts. Endpoint disabled if fails to ack over consecutive days.
- Implemented: check header `X-Finnhub-Secret == ***REMOVED***50`, return 200 JSON immediately before logic, 401 for wrong secret, log to `~/.hermes/webhook_events.jsonl`
- **Cloudflare Tunnel:** ingress `webhook.smitpatel.net -> http://localhost:8644` MUST be before catch-all `- service: http_status:404`. Stray duplicate `service: http://localhost:8644` after catch-all caused `activating (auto-restart) Result: exit-code` + ERR_NAME_NOT_RESOLVED 1033 / 530 HTML — fixed by removing stray line. DNS CNAME via `cloudflared tunnel route dns pi-tunnel webhook.smitpatel.net`
- **Subscription:** `hermes webhook subscribe finnhub-earnings --events earnings --secret d7cphh...k50 --deliver origin` → `~/.hermes/webhook_subscriptions.json` dict keyed by name, route `/webhooks/{name}`
- **Public URL:** `https://webhook.smitpatel.net/webhooks/finnhub-earnings` Secret `***REMOVED***50` Health `/health` → `{"status":"ok","service":"manual-webhook-hermes"}`

## Live test result 2026-08-03 ~13:28 PDT
- User triggered via Finnhub dashboard
- Server log line 6: ts 1785788909.425486 name finnhub-earnings path /webhooks/finnhub-earnings payload
```json
{"data":[{"date":"2020-03-03","eps_actual":17.5,"eps_estimate":15.4,"revenue_actual":55000000,"revenue_estimate":54000000,"symbol":"AAPL"}],"event":"earnings"}
```
- `secret_header_present: true` → header validation OK
- Response: 200 JSON `{"status":"ok","matched":"finnhub-earnings"}` before logic → prevents Finnhub disabling
- Shape confirms real Finnhub: `event: "earnings"` + `data` array with `symbol, date, eps_actual, eps_estimate, revenue_actual, revenue_estimate` — not simple `{symbol,date,type}` we used for manual test earlier
- Earnings cache still has CSCO 2026-08-19 blocked (historical 2020-03-03 doesn't invalidate current), correct behavior

## Next steps for 10% cash sleeve
- Webhook clears `logs/earnings_cache.json` on earnings event for held symbols to force refetch next cron (3x/day 10:05/13:05/15:35 ET)
- Polling already covers 99% (6h cache), webhook adds real-time edge if earnings announced 7am ET and cron sold yesterday
- Optional Telegram alert when earnings event symbol ∈ current short puts (e.g., CSCO) — not yet implemented, would need payload.symbol in positions list → send_message

## Links
- Finnhub webhook docs: https://finnhub.io/docs/api/webhooks
- Hermes webhook docs: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks
- Config: `config/webhook_config.json` saves public_url, local_url, secret, test curl
