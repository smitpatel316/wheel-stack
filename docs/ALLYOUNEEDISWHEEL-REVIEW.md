# AllYouNeedIsWheel — Comparative Review

**Repo reviewed:** https://github.com/xiao81/AllYouNeedIsWheel (shallow clone, Aug 14 2026)
**Reviewer:** Atlas, at Smit's request
**Purpose:** find gaps in our wheel-stack engine + upgrade ideas for our Optionable dashboard.

---

## 1. What their system actually is

A **semi-manual** wheel assistant on Interactive Brokers (not Alpaca): Flask API + SQLite +
Bootstrap UI, ~3,900 LOC. The human drives — the app recommends and the human clicks Execute.
There is **no autonomous strategy loop, no scheduler, no filters, no risk caps**.

Core pieces:
- `core/connection.py` (946 LOC) — IB/TWS connection, option-chain fetch with **IB model Greeks**
  (delta/gamma/theta/vega/IV), frozen-vs-live market-data switching, portfolio/account summary.
- `api/services/options_service.py` (1,121 LOC) — OTM strike selection, option-chain processing,
  order execution, pending-order status reconciliation.
- `api/services/portfolio_service.py` — positions, account summary, **weekly option income**.
- `frontend/` — Dashboard (account summary + positions + pending orders), Portfolio, Rollover pages.
- `db/database.py` — orders + recommendations tables (orders carry Greeks, prices, execution detail).

## 2. Things they do that we don't (gaps worth adopting)

### 2.1 Limit-price fallback ladder instead of mid→market
`options_service.py` `execute_order` (~L183-225): computes limit price as mid → bid → 90% of ask
→ last → stored premium → 1%-of-strike floor, with a $0.05 minimum. They also re-fetch live
quotes if the stored bid is ~0 before pricing. **Ours:** limit-at-mid, then market fallback after 8s.
Their ladder avoids market-order slippage on illiquid contracts; ours guarantees fills (Smit's
preference). Hybrid idea: before falling back to a naked market order, walk the ladder once
(mid+tick). *Priority: low — Smit prefers fills.*

### 2.2 They wait for Greeks instead of accepting missing ones
`connection.py` L410-437: polls up to ~5s per contract until `modelGreeks` and IV arrive.
**Ours:** whatever the Alpaca snapshot has, once — this week's logs show ~44% of the F chain
rejected as `no_delta` during roll scans. A short retry/refetch pass for missing greeks, or a
"delta unknown → treat as data gap, not reject" distinction, directly attacks our roll-candidates-
unavailable problem. *Priority: HIGH.*

### 2.3 Margin/liquidity surfaces are front-and-center
Dashboard account card shows **Excess Liquidity, Initial Margin, and a leverage % bar**
(`account_summary.html`, `portfolio_service.get_portfolio_summary`). Our options-buying-power only
existed in logs — which is exactly why the SGOV/options-BP mismatch ran silently until orders
started failing. *Priority: HIGH (dashboard).*

### 2.4 Persistent order lifecycle with broker reconciliation
Every order is saved to SQLite as `pending`, executed → `processing`, then
`check_pending_orders` (options_service.py L698-820) polls the broker and reconciles
Filled/Cancelled back into the DB, including avg fill price and commission.
**Ours:** fire-and-forget within a run; anything filling after the run ends is only caught
indirectly by the next position sync. A small order table (submitted/filled/cancelled + fill price)
would close the "phantom close price" class of bugs for good. *Priority: MEDIUM-HIGH.*

### 2.5 Frozen/stale-data indicator in the UI
`account.js` `updateDataStatusIndicator`: badges when data is frozen (market closed) vs live.
**Ours:** no freshness signal anywhere. A "last scan 13:05 ET · 15 candidates · sources ok" strip
on Optionable would have made the week-long silent liquidity-filter failure visible on day one —
the empty scan would have been *on the dashboard*, not just in a log nobody read. *Priority: HIGH (dashboard).*

### 2.6 Explicit per-ticker error returns instead of silent empties
Throughout `_process_ticker_for_otm` (L394-505): every failure path returns `{'error': ...}` per
ticker and the UI renders a visible "no data" state. Contrast with our liquidity filter returning
an empty list that sailed downstream as "No symbols found". We fixed the instance; their pattern
is the cultural version of the fix. *Adopted in spirit via the new [DATA]/[BP] logging; a scan-funnel
panel would finish the job.*

### 2.7 Rollover as a paired, human-readable ticket
Their Rollover page shows BUY TO CLOSE (at ask) and SELL TO OPEN (at mid) side by side, delta/IV
on both legs, user-selectable target expiration and OTM% (`rollover.js` L628-780). **Ours:**
fully automated with a debit cap — better for autonomy, but opaque. A read-only "roll monitor"
panel (position at risk → candidates considered → net credit/debit → action taken) gives the same
transparency without giving up automation. *Priority: MEDIUM (dashboard).*

### 2.8 Weekly income summary
`get_weekly_option_income` (portfolio_service.py ~L180+): premium from short options expiring
this Friday, count, and total put notional expiring. Cheap to compute from positions we already
sync. *Priority: quick win (dashboard).*

### 2.9 Readonly connection flag
`connection.json` `readonly: true` blocks order execution at the connection layer. Our equivalent
is `IS_PAPER=true` + paper keys. Not a gap — theirs is a seatbelt for a manually-driven app on a
live broker; ours is structural (paper account). No action.

### 2.10 Standard-strike rounding
`_adjust_to_standard_strike` (L82) snaps computed strikes to exchange increments. Our delta-band
selection from listed contracts makes this unnecessary. No action.

## 3. Things ours does that theirs lacks (the full picture for Smit)

Their repo has **none** of: fundamentals screening (P/E, D/E, growth), earnings blackout windows,
dividend-risk call blocking, IV-rank regime analysis, VIX regime-adaptive sizing, delta/yield/score
contract ranking, a risk cap (our $90k MAX_RISK), automated 50% profit-taking, automated
assignment-avoidance rolling, an SGOV/SPAXX cash sweep, fully autonomous scheduled runs, or paper
vs live separation. It is a good *manual cockpit*; ours is an *autopilot*. The gap analysis above
is mostly about adopting their cockpit instruments, not their engine.

## 4. What would have caught our recent bugs earlier

| Our bug | Their practice that addresses it |
|---|---|
| Liquidity filter silently dropped all symbols for a week | Per-ticker explicit error returns (2.6) + a visible "no data" UI state. The equivalent for us: the new `[DATA]` logging **plus** a dashboard scan-funnel panel — logging alone still requires someone to read logs. |
| Options-BP vs SGOV mismatch (AMD $43k vs $13k BP) | Excess-liquidity/margin front-and-center on the dashboard (2.3). BP was invisible until orders 403'd. |
| 44% no_delta roll rejections | Wait-for-greeks polling (2.2); missing greeks treated as a data problem, not a filter outcome. |
| Phantom $0 close-price risk (already patched) | Persistent order table reconciled against broker fills (2.4) — the fill price is recorded from the broker, not inferred. |

## 5. Optionable dashboard upgrades, ranked

**Quick wins (hours):**
1. **Capital card:** cash, options buying power, risk used vs $90k cap, SGOV value + projected
   monthly yield (we already log `$439/mo` — just surface it). ← catches BP problems visually.
2. **"Expiring this week" strip:** premium expiring, positions count, put notional (their 2.8).
3. **Distance-to-strike coloring** on positions: green/yellow/red by % OTM with delta + DTE shown
   (their `formatPercentage` proximity coloring, rollover.js L20-33).

**Medium (a day or two):**
4. **Scan-funnel panel:** last run per symbol — in → survived price/BP → survived fundamentals →
   contracts rejected by reason → trade/skip. This is the observability the liquidity bug demanded.
5. **Data-freshness badge:** timestamp of last scan + which sources responded (their 2.5).
6. **Roll monitor:** ITM-approaching positions, candidates considered, net debit/credit, outcome.

**Larger projects (only if Smit wants them):**
7. **Order ledger:** persistent submitted/filled/cancelled table with broker reconciliation (2.4).
8. **Manual action layer** (one-click roll/close approval from the dashboard) — their core model;
   conflicts with full autonomy, so only as an opt-in hybrid mode.

## 6. Bottom line

Adopt the **instruments**, not the engine: Greeks-with-retry (2.2), order ledger (2.4), and the
three dashboard surfaces (capital/BP card, scan funnel, freshness badge) are where their repo beats
us. Everything strategic — screening, sizing, rolling, sweeping, autonomy — ours already does and
they don't attempt.
