# Wheel Stack Runbook

How to run the options wheel from cold, keep it healthy, and (eventually) take
it live. Everything here is machine-generic: any Linux host with Python 3.10+
and node/bun works. The one-stop entry point is `./wheel`.

## Cold start on a fresh machine

```bash
git clone https://github.com/smitpatel316/wheel-stack.git
cd wheel-stack
./wheel setup          # venv + deps + .env scaffold (+ dashboard build if the
                       # Optionable checkout sits next to this repo)
# edit .env: ALPACA_API_KEY / ALPACA_SECRET_KEY / FINNHUB_API_KEY / ALPHA_VANTAGE_API_KEY
./wheel doctor         # pre-flight: must be all green before going unattended
./wheel start          # webhook receiver (:8644) + Optionable dashboard (:8096)
./wheel cron-install   # 3 engine runs per market day: 10:05 / 13:05 / 15:05 ET, Mon–Fri
```

The dashboard is a sibling checkout of Optionable; point at it with
`OPTIONABLE_DIR` if it isn't at `../optionable-src` or `../optionable`.
Its data lives in `DATA_DIR` (default `./data/optionable`) — back that up.

## Configuration: env wins, always

Every live-relevant knob in `config/params.py` can be overridden from the
environment or `.env` — no source edits, ever, for a deployment:

| Env var | Default | What it gates |
|---|---|---|
| `MAX_RISK` | 90000 (fallback) | risk-cap ceiling; live cap is dynamic from account liquidity |
| `MIN_PREMIUM` | 0.20 | min option premium per contract |
| `SCORE_MIN` | 0.02 | min candidate score |
| `WATCHLIST` | `config/symbol_list.txt` | comma-separated tickers, replaces the file |
| `SGOV_ENABLED` | false | Alpaca-paper cash sweep; OFF for any real broker |
| `IS_PAPER` | true | paper vs live Alpaca account |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | — | broker credentials (env only, never in git) |
| `FINNHUB_API_KEY` / `ALPHA_VANTAGE_API_KEY` | — | fundamentals data (each is the other's fallback) |
| `OPTIONABLE_URL` | http://localhost:8096 | dashboard sync target |
| `EARNINGS_SOURCE_URL` | unset (off) | base URL of the Finnhub webhook receiver (e.g. `http://<pi-host>:8744`); when set, each run pulls its invalidation state (see Fail-open sync) |
| `SYNC_OUTBOX_DIR` | `state/sync-outbox/` | durable outbox for dashboard-bound payloads |
| `SYNC_PUSH_TIMEOUT` | 5 | seconds; outbox delivery timeout |
| `EARNINGS_SOURCE_TIMEOUT` | 5 | seconds; earnings-state pull timeout |

Also overridable: `DELTA_MIN/MAX`, `YIELD_MIN/MAX`, `EXPIRATION_MIN/MAX`,
`OPEN_INTEREST_MIN`, and the feature switches (`EARNINGS_ENABLED`,
`DIVIDEND_ENABLED`, `FUNDAMENTALS_ENABLED`, `GROWTH_BLOCK_ENABLED`,
`IV_RANK_ENABLED`, `LIMIT_ORDER_ENABLED`, `RH_MCP_ENABLED`,
`PNL_TRACKER_ENABLED`, `SGOV_CASH_BUFFER`, `SGOV_TARGET_PCT`).

## Health verification

- `./wheel doctor` — hard checks: broker reachable + ACTIVE + no margin
  debit, paper/live key consistency, Optionable reachable, data feeds up.
  Exits 1 on any hard failure. Run it after every reboot and before going
  unattended.
- `./wheel status` — are the webhook and dashboard processes up right now.
- `logs/` — `cron.log` for scheduled runs, `run-manual.log` for `./wheel run`,
  `webhook-server.log`, `dashboard.log`.
- `state/funding_queue.json` — candidates queued for next-day funding (T+1).
  Should drain on its own; entries expiring repeatedly = chronic underfunding.

## After a reboot / process wipe

1. `./wheel status` — restart with `./wheel start` if the webhook/dashboard died.
2. `./wheel doctor` — confirm broker + dashboard + feeds before the next run.
3. Check the last engine run in `logs/cron.log` actually completed (look for
   the run summary, not just a start line).
4. The engine itself is cron-driven, not a daemon — nothing else to restart.

## Fail-open sync (engine host ↔ dashboard host)

Canonical state is the engine journal (`logs/`, `state/`) plus the Alpaca
broker. The Optionable dashboard is only a replica. **If the dashboard host
(Pi) is down, runs proceed normally — nothing halts and no trade record is
lost.** The two mechanisms:

**Sync outbox** (`core/sync_outbox.py`)
- Every trade record the engine sends to Optionable is written FIRST to
  `state/sync-outbox/` — one JSON file per payload, atomic tmp+rename, with a
  stable `syncId` embedded in the trade's `notes` — and only then pushed to
  `OPTIONABLE_URL` with a ≤5s timeout. A failed push stays queued; the push
  can never raise into the engine.
- Draining: each engine run drains the outbox at start, and again after a
  successful end-of-run sync, oldest-first. An item is deleted only after a
  2xx ack, a duplicate-style 400/409, or proof the receiver already has it
  (`syncId` in notes, or ticker/strike/expiry/type tuple match) — so
  re-delivery after a crash never double-records. If the dashboard is
  unreachable the drain aborts after the first failure and retries next run.
- Inspect: `ls state/sync-outbox/` (one file per pending payload, `cat` to
  read it). Replay: just `./wheel run` with the dashboard up. Corrupt items
  are quarantined to `*.bad`. Deleting a file drops that payload from the
  dashboard only — the engine journal still has the trade.
- Log lines: `grep '\[SYNC\]' logs/cron.log`.
- Deliberately NOT outboxed: the end-of-run reconciliation syncs (equity,
  closed trades, activities) and per-run dashboard telemetry — they are
  recomputed from broker state every run, so they self-heal, and replaying a
  stale snapshot would corrupt the dashboard's current view.

**Earnings pull** (`core/earnings_source.py`)
- With `EARNINGS_SOURCE_URL` set, each run starts by GETting
  `<url>/earnings/state` (≤5s). If the webhook receiver has seen a newer
  Finnhub event than the local cache, the local cache is cleared so the run
  refetches from Finnhub; the applied marker lives in
  `state/earnings-source-state.json`.
- On ANY failure (unreachable, timeout, non-2xx, bad JSON) the run logs a
  grep-able `[EARNINGS-SOURCE]` WARNING and continues with exactly the old
  behavior: fresh cache → 48h stale cache → `state/earnings-last-good.json`
  snapshot (new: survives >48h outages) → Alpha Vantage fallback. With the
  env var unset it is a complete no-op.
- The webhook receiver (`scripts/webhook_server.py`) serves
  `/earnings/state` and takes its port from `WEBHOOK_PORT` — default 8644
  unchanged; run the Pi copy with `WEBHOOK_PORT=8744`. Its local cache-clear
  on each event is unchanged (only relevant when engine and receiver share a
  host).

## Go-live checklist (Ladder Phase 1)

Going live needs Smit's explicit go-ahead. When he says go:

- [ ] Copy `config/phase1-live.env.example` → live env file, fill in **live**
      Alpaca keys (`AK…`), `IS_PAPER=false`, `MAX_RISK=1000`,
      `MIN_PREMIUM=0.10`, `SCORE_MIN=0.01`, `WATCHLIST=F`,
      `SGOV_ENABLED=false`.
- [ ] Confirm Alpaca live account has options trading enabled and ~$1,000
      settled cash. No margin. Ever.
- [ ] `./wheel doctor` all green against the **live** keys (it refuses
      PK/AK mismatches and fails on margin debit).
- [ ] One supervised `./wheel run` in market hours; verify the order in the
      Alpaca dashboard before letting cron take over.
- [ ] `./wheel cron-install` on the live host.
- [ ] First week: review every run log daily. Report trades and problems.
- [ ] Never reuse the $100k paper params for live — each Ladder phase gets
      its own env file.

## Non-negotiables

- Never carry a margin debit (doctor hard-fails on negative cash).
- Never widen the watchlist or loosen fundamentals to force deployment —
  idle cash is fine.
- `SGOV_ENABLED=false` anywhere real money lives.
- Real credentials never touch git — this repo is public.
