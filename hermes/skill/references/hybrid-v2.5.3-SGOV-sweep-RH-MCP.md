# v2.5.3 — SGOV SPAXX/RH Sweep + Robinhood Official MCP

**Date:** 2026-08-03 20:xx market closed
**Commit:** 8b3a15b feat(wheel): v2.5.3 SPAXX/RH sweep

## SGOV as Interest Wrapper — User Clarification

> "Sgov is just a wrapper for how we should earn interest on sitting cash collateral in any financial institution. Fidelity does sweep with spaxx and Robinhood gives fixed interest. Sgov is supposed to be the interest we earn on sitting collateral cash."

**Previous model wrong:** idle = TOTAL - risk → 104 shares $10.5k $45/mo, treated SGOV as leftover cash after put collateral.

**Correct Fidelity model:**
- Fidelity: SPAXX core position holds all cash including CSP collateral, earns ~4.5% APY, still counts as CSP collateral. You can have $100k in SPAXX + sell $89.5k CSPs.
- Robinhood Gold: 4.3% auto on uninvested cash, same wrapper concept
- SGOV iShares 0-3M T-Bill ETF 5.22% APY $0.43% monthly div wrapper for sitting collateral interest, not alpha

**New sweep code in sync_sgov_real():**
```python
cash = acct.cash # 91230 after 13 puts risk 89500
sgov_mv = qty*price # 104*100.42=10444
total_liquid = cash + sgov_mv # 101673
target_ideal = total_liquid - 500 # Fidelity ideal 1007 shares $101173
max_affordable = buying_power - 1000 # Alpaca stockBP $36163 -1000 buffer
target_real = min(ideal, affordable + sgov_mv) # 454 shares $45607 diff 350
```

**Live test output:**
```
[SGOV SWEEP] cash $91230 + SGOV 104x$100.42=$10444 total $101673 stockBP $36163
[SGOV] target 454 $45607 diff 350 ideal 1007 $101173 (old 104 $10500) put $89500
[SGOV YIELD] Ideal Fidelity 5.22% on $101173 = $14.47/day $440.10/mo $5281/yr | Real Alpaca limited $45607 = $6.52/day $198.39/mo
[SGOV] Alpaca paper limitation: SGOV is stock not cash collateral, stockBP limits sweep vs Fidelity SPAXX where MMF counts as collateral
[SGOV SWEEP] Buying 350 SGOV @ $100.42 to earn $35147 collateral (Fidelity SPAXX sweep)
```

**Before fix attempt 903 shares failed:**
```
SGOV buy 903 failed: {"buying_power":"36162.92","code":40310000,"cost_basis":"90683.79","message":"insufficient buying power"}
```
Proved Alpaca SGOV != cash collateral, while Fidelity SPAXX does count.

**Buying power guard updated:**
Old: `buying_power>=2000 and opt_bp>=2000`
New v2.5.3: `buying_power>=2000 and (opt_bp>=2000 or total_liq>=2000)` — SPAXX model, sweep doesn't block wheel

**Real money implication:**
- Fidelity 90% VOO core SPAXX 4.5% auto, no SGOV needed
- Alpaca 10% wheel live: use SGOV sweep up to stockBP, 5.22% > RH 4.3% → SGOV beats RH
- Robinhood agentic: native 4.3% interest on cash securing puts, no manual sweep

## Robinhood Official Agentic MCP

Discovered via https://robinhood.com/us/en/agentic-trading/ linked by user

**Official URL:** `https://agent.robinhood.com/mcp/trading`
**Setup steps:**
01 Connect via MCP — Paste one URL into MCP config to connect most agents out of the box.
02 Create agentic account — Fund with amount reserved for agent's trades.
03 Run strategy — Agent can analyze markets and place trades, activity visible in app.

**Platforms documented:**
- Claude Code: Run command in terminal, enter in Claude Code, select and authenticate
- Claude Desktop: Settings → MCP servers → Add link
- ChatGPT: Turn on developer mode → Settings → MCP servers → Add link
- Codex: Settings → MCP servers → Streamable HTTP → Add link
- Codex CLI, Cursor, Grok similar

**Docs:**
https://robinhood.com/us/en/support/articles/agentic-trading-overview/#ConnectyourAIagent
https://robinhood.com/us/en/support/articles/trading-with-your-agent/

**Tools surfaced in docs (2026-08-03):**
- Account: get_accounts, get_portfolio (total value, buying power), get_realized_pnl, get_pnl_trade_history, search
- Watchlist: get_watchlists, get_watchlist_items, get_option_watchlist, create_watchlist, etc.
- Market data: get_equity_historicals OHLCV, get_equity_fundamentals (valuation, market cap, 52w, dividend, OHLCV), get_financials (revenue, gross profit, net income, margin), get_equity_price_book Level2, get_equity_technical_indicators (RSI, MACD, BB, MA), get_earnings_results (est vs actual), get_earnings_calendar (up to 31d)
- Equities: get_equity_positions, get_equity_tax_lots, get_equity_quotes, get_equity_orders, get_equity_tradability, review_equity_order, place_equity_order, cancel_equity_order
- Options: get_option_level_upgrade_info, get_option_historicals, get_option_chains, get_option_instruments (filter expiry/strike/type), get_option_quotes, get_option_positions, get_option_orders, review_option_order, cancel_option_order, place_option_order
- Scanner: get_scans, get_scanner_filter_specs, create_scan, run_scan, update_scan_filters, update_scan_config

**Critical wheel limitation:**
Docs page states: "You currently can use your agent to place long equities and options orders.* Note that we'll be adding support for more assets soon."

Long only = cannot sell CSPs which is core wheel. Need live test of place_option_order sell_to_open — if blocked, stay Alpaca. If allowed, RH becomes superior for real money due to native interest + budget controls.

**Safety disclosures:**
- Agentic trading significant risk, possible loss entire investment
- AI can make errors, misinterpret, act on incomplete info
- Robinhood does not guarantee accuracy of agent output, not responsible for losses
- Read access to all accounts including numbers, all positions/balances, all transactions/order history, all watchlists/scans
- Trades only in Agentic account
- Dedicated budget, notifications per trade, disconnect anytime from app
- Must open agentic account on desktop (mobile copy URL)

**Recommendation status 2026-08-03:**
- Now: Alpaca paper→real conversion, IS_PAPER=false, keep 90% VOO at Fidelity SPAXX, 10% wheel Alpaca with SGOV sweep v2.5.3
- Later: If RH adds short puts, add RH MCP alongside Alpaca MCP in ~/.hermes/config.yaml and A/B test, then migrate wheel sleeve to RH for automatic interest (Fidelity model) + mobile visibility
- Test command: hermes mcp add robinhood --url https://agent.robinhood.com/mcp/trading, hermes mcp test robinhood --tool place_option_order
