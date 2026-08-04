# Alpaca Websocket Streaming — Real-Time Trade Sync

## Why Cron Polling Is Not Enough
Cron ET 10:05/13:05/15:35 (PDT 7:05/10:05/12:35) has 10-min delay and misses:
- Intraday fills between polls
- Overnight assignments / expirations until next morning
- SGOV partial fills

Alpaca Trading API offers `wss://paper-api.alpaca.markets/stream` (paper) and `wss://api.alpaca.markets/stream` (live) with `trade_updates` channel.

Doc: https://docs.alpaca.markets/us/docs/websocket-streaming (Readme.io shell — actual content client-side, SDK is source of truth).

## SDK (alpaca-py 0.43.5)

```python
from alpaca.trading.stream import TradingStream
stream = TradingStream(api_key, secret_key, paper=True)
stream.subscribe_trade_updates(handler)
await stream._run_forever()
```

Structure `alpaca/trading/stream.py`:
- `_endpoint = BaseURL.TRADING_STREAM_PAPER if paper else LIVE`
- `_auth()` sends `{"action":"authenticate","data":{"key_id":...,"secret_key":...}}`
- `_dispatch` checks `msg['stream'] == 'trade_updates'` -> `_cast` -> `TradeUpdate(**msg['data'])`
- `_subscribe_trade_updates` sends `{"action":"listen","data":{"streams":["trade_updates"]}}`
- `_consume` loop `await ws.recv()` with 5s timeout, `_stop_stream_queue` for graceful close
- `_run_forever` waits for handler subscribed, then connect/auth/subscribe/consume with auto-restart on WebSocketException

TradeUpdate fields: `event` (new|accepted|fill|partial_fill|canceled|expired|done_for_day|rejected), `order` (symbol, side, qty, filled_qty, filled_avg_price, asset_class, order_type), `price`, `timestamp`.

## Implementation Pattern — Pi Homelab Service

File `scripts/alpaca_stream_sync.py`:
- Async handler `on_trade_update` handles both pydantic `TradeUpdate` and raw dict fallback (when `raw_data=True`).
- Extract `event`, `order.symbol`, `side`, `qty`, `filled_qty`, `filled_avg_price`, `asset_class`.
- Filter:
  - `fill`/`partial_fill`:
    - If OPTION (asset_class contains OPTION or OCC len>15 with P/C) → if sell side → `push_trade_to_optionable(symbol, filled_avg, contracts=qty)`
    - If buy-to-close → `sync_closed_trades(client)`
    - If STOCK → `sync_alpaca_equity_to_optionable + sync_sgov_to_optionable`
    - After fill `await asyncio.sleep(2)` then `sync_closed_trades` for position settle
  - `expired`/`canceled`/`done_for_day`/`rejected` → `sync_closed_trades` to mark Expired
- Outer `run_stream()` loop with `while True: try: TradingStream... _run_forever() except: sleep 10 restart`

Systemd user service `~/.config/systemd/user/alpaca-stream.service`:
```ini
[Unit]
Description=Alpaca TradingStream -> Optionable real-time sync
After=network-online.target
[Service]
Type=simple
WorkingDirectory=/home/smitpatel316/options-wheel
ExecStart=/home/smitpatel316/options-wheel/.venv/bin/python scripts/alpaca_stream_sync.py
Environment=OPTIONABLE_URL=http://localhost:8096
Environment=TZ=America/Los_Angeles
Restart=always
RestartSec=10
StandardOutput=append:/home/smitpatel316/options-wheel/logs/stream.log
StandardError=append:/home/smitpatel316/options-wheel/logs/stream.log
[Install]
WantedBy=default.target
```
Enable: `systemctl --user daemon-reload && enable --now alpaca-stream`

Verify: `systemctl --user status alpaca-stream` → active running, `logs/stream.log` shows `connected to: BaseURL.TRADING_STREAM_PAPER`, subscribed.

## Account Activities — Dividends / Interest Gap

Options assignment / corporate actions and SGOV dividends are NOT in trade_updates. They come via:
- REST: `GET /v2/account/activities` with `activity_type=DIV|INT|FEE|TAXF|OPTION_ASSIGNMENT` etc
- SSE streaming: `GET /v2beta1/data/account/activities` or `/v2beta1/account/activities_stream`? Docs `activity-sse.md`

SDK 0.43.5 pitfall: `from alpaca.trading.requests import GetAccountActivitiesRequest` fails — class renamed/removed, `TradingClient.get_account_activities` missing. Workaround: raw REST `https://paper-api.alpaca.markets/v2/account/activities` with headers `APCA-API-KEY-ID`, `APCA-API-SECRET-KEY`.

For paper with no div history, manual Fund Journal entry in Optionable is sufficient. For production, add hourly polling job that fetches DIV/INT and POSTs to `/api/fund-transactions` {type: dividend/interest, amount, date, description}.

## Why This Matters for wheel.smitpatel.net

Before: 3 polls/day → Optionable Trade Log stale until next cron, assignment appears next morning.
After: seconds latency → Portfolio P/L, Monthly, Ticker, Deployed Capital update real-time when Alpaca fills.

Keep cron as safety fallback (strategy log + SGOV dynamic + extra equity sync) — stream may drop on Pi network blip, auto-restart recovers.
