# Robinhood Agentic Trading — Official MCP 2026-08-03

Official URL discovered from https://robinhood.com/us/en/agentic-trading/

**MCP URL:** `https://agent.robinhood.com/mcp/trading`
Transport: Streamable HTTP (SSE)

**Setup:**
- Connect via Claude Code: `claude mcp add --transport http robinhood https://agent.robinhood.com/mcp/trading`
- Claude Desktop: Settings → Connectors → Add MCP link
- ChatGPT / Codex / Cursor / Grok: Settings → MCP servers → Streamable HTTP → paste URL
- Then desktop onboarding opens Agentic account (individual investing, up to 10 per user, dedicated budget)

**What agent can do (from /trading-with-your-agent/):**
- Read: get_accounts, get_portfolio (total value, buying power), get_realized_pnl, get_pnl_trade_history, search, get_watchlists, get_watchlist_items, get_popular_watchlists
- Market data: get_equity_historicals (OHLCV), get_equity_fundamentals (valuation, mcap, 52w, div, OHLCV), get_financials (revenue/gross/net margin quarter/year), get_equity_price_book (L2), get_equity_technical_indicators (RSI/MACD/BB/MA), get_earnings_results (est vs actual), get_earnings_calendar (31d window), get_indexes, get_index_quotes
- Equities: get_equity_positions, get_equity_tax_lots, get_equity_quotes (20 symbols), get_equity_orders, get_equity_tradability, review_equity_order (simulate), place_equity_order, cancel_equity_order
- Options: get_option_level_upgrade_info, get_option_historicals, get_option_chains, get_option_instruments (filter expiry/strike/type), get_option_quotes, get_option_positions, get_option_orders, review_option_order, cancel_option_order, place_option_order
- Scans: get_scans, get_scanner_filter_specs, create_scan, run_scan, update_scan_filters, update_scan_config

**Critical wheel limitation Aug 2026:**
Docs: "You currently can use your agent to place long equities and options orders.* Note that we'll be adding support for more assets soon."
- If long-only, short puts (sell CSPs) not supported → wheel impossible. Need live test of place_option_order with side=sell_to_open.
- Alpaca currently supports short puts proven 13 CSPs risk $89.5k + rolls.

**Safety:**
- Dedicated Agentic account with budget, notifications per trade, disconnect anytime in app
- Read access to all accounts/balances/transactions/watchlists (IMPORTANT disclosure)
- Trades only in Agentic account, user ultimately responsible, AI can misinterpret
- Ref: Reference No. 5762361 trading, 5704311 overview

**Comparison for $1k-$100k wheel:**
- Alpaca paper→real: official short puts, 62 MCP tools, limit-at-mid 8s, spread filter $0.15/12%, closer 50%, roller 3% close-before-open 2s, webhook Finnhub X-Finnhub-Secret real-time, SGOV sweep 5.22% ideal vs paper 0%
- Robinhood agentic: MCP official, native interest 4.3% on sitting collateral (no SGOV wrapper needed — matches user's SGOV-as-SPAXX-wrapper mental model perfectly), mobile monitoring, budget caps, but currently long-only may block wheel until short selling added.

**Recommendation logic:**
- Now Aug 2026: Convert Alpaca paper PA3W ($100k equity $99.8k 13 CSPs + SGOV 104) to live for wheel — flip IS_PAPER=false, keep MAX_RISK 90k for $100k or 900 for $1k sleeve.
- Later when Robinhood adds short puts: migrate, benefit native interest (SGOV wrapper disappears), use SGOV sweep code as fallback for Alpaca.

**User context:** User manages 90% self VOO core at Fidelity (SPAXX sweep), 10% wheel sleeve agentic. $1000 end-of-month start recommended F $10P only, MAX_RISK 1000, watchlist ["F"], premium $0.15-0.25.

**Test command once connected:**
```bash
hermes mcp add robinhood --url https://agent.robinhood.com/mcp/trading --transport http
hermes mcp test robinhood --tool get_portfolio
hermes mcp test robinhood --tool get_option_chains --args '{"symbol":"F"}'
```
Check if place_option_order accepts sell_to_open for puts.
