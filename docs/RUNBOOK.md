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
