# Wheeler Integration — Build Log & Schema Notes

## Session: 2026-08-02 - Deploy Wheeler to budupi Pi + bridge to options-wheel

### Repo
- Source: https://github.com/MarkT1065/wheeler - 47★, Go 1.24, 11.5k LOC, SQLite, Chart.js
- Purpose: **Tracker only** (no execution), Treasury collateral mgmt, multiple DBs, CSV import, Polygon.io
- Schema: symbols(PK symbol), options(id, symbol FK, type Put/Call, opened, closed, strike, exp, premium, contracts, exit_price, commission), long_positions, dividends, treasuries(cuspid PK), metrics, settings(POLYGON_API_KEY)
- Unique indexes: `idx_options_unique` on (symbol,type,opened,strike,expiration,premium,contracts) → idempotent pushes
- Premium example: wheel_strategy_example.sql shows `0.80, 0.95, 2.10, 1.20` = per-share bid, not total
- Commission: $0.65 per contract auto in Create()
- Models: OptionService, LongPositionService, TreasuryService, SymbolService etc

### Build Failures & Fixes

1. **Go not installed on Pi**: `go: command not found`. Use golang:1.24-alpine container for builds.
2. **CGO_REQUIRED**: First build `CGO_ENABLED=0` static → `go-sqlite3 requires cgo to work. This is a stub`. Must `apk add gcc musl-dev && CGO_ENABLED=1 go build`.
3. **Docker builder snapshot bug**: Pi Docker overlay failed `failed to commit ... snapshot does not exist: not found` on `RUN apk add`. Occurs on low disk / high load. Workaround: build binary in separate `docker run --rm -v ... golang:1.24-alpine ...`, then final image only COPYs binary + internal templates, avoids apk add layer issues. Second attempt still hit bug, solved with two-stage: build bin outside, then Dockerfile copies bin + internal.
4. **Binary naming**: `wheeler.bin` (non-CGO) vs `wheeler.cgo.bin`. Need CGO one dynamically linked `/lib/ld-musl-aarch64.so.1`, 16M vs 13M static.
5. **Entrypoint permission**: First container as `appuser` failed `unable to open database file`. Volume mount host files owned by `dhcpcd/_ssh` (1000) vs container appuser UID mismatch. Fixed with root entry.sh that `chown -R appuser:appuser /app/data` then `su-exec appuser /app/wheeler`.
6. **SQLite WAL readonly**: Host python trying `DELETE FROM options` while container holds lock → `attempt to write a readonly database`. Must stop container or use `docker exec -u root wheeler sh -c 'rm -f /app/data/wheeler.db...'` or run sqlite via docker exec.
7. **Cloudflare tunnel DNS second step**: `~/.cloudflared/config.yml` edit alone not enough, must `cloudflared tunnel route dns pi-tunnel wheel.smitpatel.net` else external 1033 / Could not resolve host. Already had 11 hosts ingress; added 12th wheel.

### Files

- `~/wheeler/data/wheeler.cgo.bin` 16M ARM64 dynamically linked
- `~/wheeler/data/wheeler.db` - SQLite main
- `~/wheeler/docker-compose.pi.yml` port 8096:8080
- `~/wheeler/Dockerfile.pi` plus entry.sh
- Container: `wheeler:pi`, name `wheeler`, restart unless-stopped, 0.0.0.0:8096->8080

### Cloudflare

Config at `~/.cloudflared/config.yml` tunnel b826eba9-c615-4358-8fb2-b6b0277ffbd3 pi-tunnel
Ingress list now includes wheel
CNAME: `wheel.smitpatel.net is already configured to route to your tunnel` after first `route dns`
Verify: `curl -sI https://wheel.smitpatel.net` → HTTP/2 200, `curl -s https://wheel.smitpatel.net/api/allocation-data` returns JSON

### Bridge

`~/options-wheel/core/wheeler_sync.py`:
- `_parse_occ('AAPL260116P00308000')` → ('AAPL','2026-01-16','Put',308.0,...)
- `push_option_to_wheeler(occ, bid_per_share, contracts=1)` → PUT symbol then POST /api/options
- Premium convention fix: previously draft multiplied *100 total, corrected to per-share directly after checking example SQL
- Idempotent success on UNIQUE text in 500 response body
- `sync_alpaca_equity_positions_to_wheeler(client)` loops positions, POST /api/long-positions if equity

Test push 3 samples → `putsByTicker [{"label":"AAPL","value":30000}, {"label":"BAC"...}]` `putROI 0.508` → verified UI works

### Integration Points

- `core/execution.py` patched: after market_sell add try push_option_to_wheeler
- `scripts/run_strategy.py` patched: after save sync equity to Wheeler
- `run_wheel_cron.sh` updated: healthcheck curl allocation-data else docker restart wheeler, then run strategy

Commit: b38f13f `integrate Wheeler tracker: auto-push CSP/CC sells to wheel.smitpatel.net:8096, sync equity positions, cron auto-restart wheeler, bridge wheeler_sync.py OCC parser + idempotent POST`

### Tomorrow Debug Jobs

Hermes cronjobs b9bea0fd8202 (06:45 PDT debug) and d284180b8658 (07:10 market-open check) scheduled for Mon Aug 3. They will also hit wheeler_alive() path.
