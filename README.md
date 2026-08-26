# wheel-stack

**An autonomous options wheel engine for Alpaca** — sells cash-secured puts, manages them to profit or assignment, then sells covered calls, with multi-layer risk screens and self-healing data feeds.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-Apache--2.0-green)
![Tests](https://img.shields.io/badge/tests-211%20passing-brightgreen)
![Mode](https://img.shields.io/badge/default-paper%20trading-orange)

## Features

- **Full wheel cycle** — cash-secured puts on fundamentally screened underlyings; on assignment, covered calls that respect your share cost basis (never sell a call below your net cost)
- **Model-first entry screens** — earnings calendar block, dividend ex-date risk, fundamentals (P/E, debt/equity, market cap, dividend yield, growth), realized-volatility regime with adaptive delta bands, liquidity trend scoring, and spread caps
- **Profit-taking closer** — buys back winners at 50% of max profit (DTE > 3), time-efficient 40% closes in the 7–21 DTE window, and locks in 75%+ gains immediately
- **Disciplined roller (v2.6)** — rolls threatened positions for a net credit only, at most twice per position lineage, then lets it ride; premium-loss alone never triggers a roll (requires < 1% OTM or |delta| ≥ 0.40); a last-day critical override still allows a small debit to avoid assignment
- **Multi-source market data with automatic fallback** — Alpha Vantage primary; on failure, fundamentals fall back to Finnhub and price history to Alpaca daily bars, with a fail-stale on-disk cache as the last resort. A dead data provider never silently disables the screens
- **SGOV cash sweep** — idle cash is swept into short-term T-bill ETF exposure instead of sitting unearning, with next-day trade pre-funding (T+1 settlement aware). Isolated behind `SGOV_ENABLED` for brokers with native cash sweeps
- **Funding queue** — candidates that exceed settled options buying power are queued and funded automatically once cash settles
- **Execution** — limit-at-mid orders with timed market fallback to cut slippage; duplicate-order guards
- **Observability** — 30-factor structured strategy log per run, plus live sync to an [Optionable](https://github.com/yomikoye/optionable) dashboard (positions, capital & collateral, scan funnel)
- **Earnings webhook receiver** — Finnhub real-time earnings alerts instantly invalidate the cached calendar (`scripts/webhook_server.py`)
- **Tested** — 211 tests including stress harness, fuzz, and regression guards for past production bugs

## Architecture

```
scripts/run_strategy.py        # one strategy cycle: scan -> close -> roll -> sell -> sweep -> sync
core/
  strategy.py                  # underlying + option filters, scoring, selection
  execution.py                 # order placement, limit-at-mid, Optionable trade sync
  closer.py                    # 50% profit-take engine
  roller.py                    # net-credit roll engine (v2.6)
  context_analyzer.py          # VIX/regime context, adaptive parameters
  earnings_calendar.py         # Finnhub earnings, cached, webhook-invalidated
  dividend_calendar.py         # ex-dividend early-assignment risk
  fundamentals.py              # balance-sheet + valuation screens
  volatility.py / liquidity.py # realized-vol regime + volume/OI trend
  data_fallbacks.py            # Alpha Vantage -> Finnhub/Alpaca automatic failover
  funding_queue.py             # T+1 next-day funding queue
  state_manager.py             # positions, exposure, roll lineage counts
  optionable_sync.py           # dashboard + P/L reconciliation bridge
  robinhood_feed.py            # optional read-only broker comparison feed
config/
  params.py                    # every strategy knob, documented
  symbol_list.txt              # watchlist
scripts/webhook_server.py      # Finnhub earnings webhook receiver
tests/                         # 211 tests: unit, stress, fuzz, regression guards
```

## Quickstart

Requires Python 3.10+ and a free [Alpaca paper account](https://app.alpaca.markets/paper/dashboard/overview).

```bash
git clone https://github.com/smitpatel316/wheel-stack.git
cd wheel-stack
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

cp config/.env.example .env
# edit .env — bring your own keys (all have free tiers):
#   ALPACA_API_KEY / ALPACA_SECRET_KEY   paper trading + market data
#   FINNHUB_API_KEY                      earnings calendar, fundamentals fallback
#   ALPHA_VANTAGE_API_KEY                fundamentals, volatility, dividends

# Run one strategy cycle (paper by default — no real money):
PYTHONPATH=. python scripts/run_strategy.py --strat-log --log-level INFO

# Run the test suite:
python -m pytest tests/ -q
```

A typical deployment schedules the run 2–3 times per market day (e.g. shortly after open, midday, and before close) via cron.

## Safety

- **Paper trading is the default** (`IS_PAPER=true` in `.env`), and the entire codebase is developed and tested against Alpaca's paper API. Treat flipping to live as a deliberate, separate decision that includes reviewing `config/params.py` (risk caps, position sizing) for the account size you actually intend to trade.
- One contract per symbol, hard `MAX_RISK` exposure cap, no margin usage by design.
- State (`state/`) and logs (`logs/`) are created at runtime and never committed.

## License

Apache-2.0 — see [LICENSE](LICENSE). Original options-wheel concept from Alpaca's examples; heavy modifications and the surrounding stack by Smit Patel.
