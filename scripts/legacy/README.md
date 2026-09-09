# scripts/legacy — quarantined, do not run

`sync_sgov.py` (moved here 2026-09-09): legacy standalone SGOV sync with a
hardcoded `TOTAL_CAPITAL=100_000` and direct Alpaca calls. It has no callers
in the repo and is superseded by `core/sgov_float.py` (v2.8 float model) via
`sync_sgov_real()` in `scripts/run_strategy.py`. Kept for reference only —
running it would fight the engine's own sweep.
