"""SGOV float model v2.8 (Smit, 2026-08-28).

Replaces the sweep-all-idle-cash model. Only the STRUCTURAL FLOAT — account
equity above the effective risk cap — is parked in SGOV:

    target_sgov_mv = max(0, account_equity - effective_risk_cap)

The effective risk cap is v2.7 dynamic MAX_RISK (cash + SGOV mv -
SGOV_CASH_BUFFER, regime-scaled via adapt_params); the caller (run_strategy)
passes the adapted cap in. Everything INSIDE the cap stays liquid cash:
deployed becomes CSP collateral, and any leftover fraction (e.g. only $89k of
a $90k cap could be deployed — the $1k slack) is never swept; it stays cash,
ready to deploy next run. As the cap rises/falls with equity and regime
scaling, the SGOV float moves down/up correspondingly. Gains compound back
into deployable cash automatically.

Broker-agnostic by design: at Robinhood the in-cap liquid bucket earns the
broker's native cash-sweep interest, but the float above the cap should sit
in SGOV there too (better yield for long-term holds) — same model, no code
change.

A $X drift below SGOV_REBALANCE_BAND is ignored (no per-run churn for small
equity/regime noise; Smit's tolerance band). Only when |target - held|
>= band do we place a rebalance order, market both ways (Smit's standing
SGOV order convention).

Never creates margin: buys are additionally capped by the $1k stock-BP
buffer and (while a funding queue is pending) by settled options-BP headroom
so a buy never starves next-day CSP funding. Never sells more than is
available after pending SGOV sells (open orders + the prefund ledger).
"""

import logging
import math

from config.params import (SGOV_CASH_BUFFER, SGOV_REBALANCE_BAND, SGOV_STOCK_BP_BUFFER,
                           SGOV_YIELD_APY)

logger = logging.getLogger(__name__)


def compute_float_target(equity: float, risk_cap: float) -> float:
    """Structural float above the deployable risk cap, floored at 0."""
    return max(0.0, float(equity) - float(risk_cap))


def decide_float_order(target_mv: float, held_qty: int, price: float,
                       band: float = SGOV_REBALANCE_BAND,
                       pending_buy_qty: int = 0, pending_sell_qty: int = 0,
                       buy_capacity_usd: float | None = None,
                       queue_bp_protect_usd: float | None = None):
    """Pure rebalance decision. Returns (action, qty, reason).

    action in {"hold", "buy", "sell"}; qty in shares (0 on hold).

    Guards (ported from the v2.5/v2.7 sweep — every one pins a real paper
    paper-bug):
    - An open SGOV BUY suppresses any new buy (duplicate order).
    - Pending sells shrink effective holdings; a further sell is capped at
      that remainder, and a buy while sells are pending is buy-back churn —
      always a hold.
    - Buys (never sells) are capped by buy_capacity_usd (stock BP minus the
      $1k buffer at the call site). Holding SGOV consumes no buying power,
      so low BP must never force a sell.
    - queue_bp_protect_usd caps buys while a T+1 funding queue is pending:
      $1 swept to SGOV is ~$1 less settled options BP for the queued CSP.
      NOTE the old queue_reserve target-shrink is gone: under the float
      model nothing inside the risk cap is ever swept, so queued-CSP cash
      stays liquid by construction — the buy cap is the only needed guard.
    """
    if price <= 0 or math.isnan(price):
        return ("hold", 0, "no usable SGOV price")
    if pending_buy_qty > 0:
        return ("hold", 0, f"open SGOV BUY {pending_buy_qty} pending - skip duplicate")

    held_mv = held_qty * price
    drift = target_mv - held_mv
    if abs(drift) < band:
        return ("hold", 0, f"drift ${drift:,.0f} within ${band:,.0f} band")

    desired_qty = math.floor(target_mv / price)
    effective_qty = max(0, int(held_qty) - int(pending_sell_qty))

    if drift >= band:
        # Want MORE SGOV -> buy, unless guards say otherwise.
        if pending_sell_qty > 0:
            return ("hold", 0,
                    f"{pending_sell_qty} SGOV sell(s) pending - buy-back would be churn")
        diff = desired_qty - held_qty
        if diff <= 0:
            return ("hold", 0, "target already met after share rounding")
        if buy_capacity_usd is not None:
            cap_qty = max(0, math.floor(max(0.0, buy_capacity_usd) / price))
            if cap_qty < diff:
                diff = cap_qty
        if queue_bp_protect_usd is not None:
            protect_qty = max(0, math.floor(max(0.0, queue_bp_protect_usd) / price))
            if protect_qty < diff:
                diff = protect_qty
        if diff <= 0:
            return ("hold", 0, "BP guards cap the buy to 0 shares")
        return ("buy", int(diff), f"float ${target_mv:,.0f} > held ${held_mv:,.0f}")

    # Want LESS SGOV -> sell at most what remains after pending sells.
    sell_qty = min(effective_qty - desired_qty, effective_qty)
    if sell_qty <= 0:
        return ("hold", 0, "pending sell already covers the reduction")
    return ("sell", int(sell_qty),
            f"float ${target_mv:,.0f} < held ${held_mv:,.0f}")


def sync_sgov_float(client, log=None, equity=None, risk_cap=None, *,
                    enabled=None, band=SGOV_REBALANCE_BAND,
                    order_fn=None):
    """Reconcile SGOV holdings with the float target. Never raises.

    `risk_cap` must be the run's EFFECTIVE cap (v2.7 dynamic base after
    adapt_params regime scaling). When omitted (tests/standalone) it falls
    back to the unscaled v2.7 dynamic base: max(0, cash + SGOV mv - buffer).
    `enabled=None` reads config.params.SGOV_ENABLED live (the kill switch,
    honored even by un-gated callers); callers may pass it explicitly.
    `order_fn` defaults to execution.place_sgov_limit_order (injected in
    tests so no order helper ever fires when disabled).
    """
    if enabled is None:
        import config.params as _params
        enabled = _params.SGOV_ENABLED
    log = log or logger
    if not enabled:
        return
    if order_fn is None:
        from core.execution import place_sgov_limit_order
        order_fn = place_sgov_limit_order
    try:
        positions = client.get_positions()
        acct = client.get_account()
        cash = float(acct.cash)
        equity_v = float(equity if equity is not None else acct.equity)
        stock_bp = float(getattr(acct, 'buying_power', 0) or 0)
        opt_bp = float(getattr(acct, 'options_buying_power', 0) or 0)

        sgov_qty = 0
        sgov_price = 100.72
        for p in positions:
            if getattr(p, 'symbol', '') == 'SGOV':
                try:
                    sgov_qty = int(float(getattr(p, 'qty', 0)))
                    sgov_price = float(getattr(p, 'current_price', sgov_price) or sgov_price)
                except Exception as e:
                    log.debug("[SWALLOWED] SGOV position field parse failed, keeping qty/price defaults: %r", e)
        try:
            latest = client.get_stock_latest_trade("SGOV")
            trade = latest.get("SGOV") if isinstance(latest, dict) else None
            if trade:
                pr = getattr(trade, 'price', None) or (trade.get('price') if isinstance(trade, dict) else None)
                if pr:
                    sgov_price = float(pr)
        except Exception as e:
            log.info(f"SGOV price fetch fallback: {e}")
        sgov_mv = sgov_qty * sgov_price

        if risk_cap is None:
            # Unscaled v2.7 dynamic base; run_strategy always passes the
            # regime-scaled cap, this is the standalone fallback only.
            risk_cap = max(0.0, cash + sgov_mv - SGOV_CASH_BUFFER)
        target_mv = compute_float_target(equity_v, risk_cap)

        # Funding-queue ledger: a pre-fund sale that already FILLED is
        # invisible to the open-orders guard while Alpaca's position view
        # lags — the sweep must subtract it (2026-08-21 double-sell).
        queue_need_pending = 0.0
        prefund_pending_qty = 0
        try:
            from core.funding_queue import FundingQueue
            _q = FundingQueue().load()
            _q.expire()
            _q.save()
            queue_need_pending = _q.pending_need()
            prefund_pending_qty = _q.pending_prefund_qty()
        except Exception as _qe:
            log.debug(f"[SGOV FLOAT] funding-queue check failed: {_qe}")

        # Pending broker orders (belt under the ledger suspenders).
        pending_buy_qty = 0
        pending_sell_qty = prefund_pending_qty
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            open_orders = client.trade_client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=50))
            pending_buy_qty += sum(int(float(o.qty)) for o in open_orders
                                   if getattr(o, 'symbol', '') == 'SGOV'
                                   and 'buy' in str(getattr(o, 'side', '')).lower())
            pending_sell_qty += sum(int(float(o.qty)) for o in open_orders
                                    if getattr(o, 'symbol', '') == 'SGOV'
                                    and 'sell' in str(getattr(o, 'side', '')).lower())
        except Exception as e:
            log.debug(f"Open order check failed: {e}")

        # stock-BP buffer constrains PURCHASES only; and while a queue is
        # pending, buys may eat only BP the queue doesn't need.
        buy_capacity = max(0.0, stock_bp - SGOV_STOCK_BP_BUFFER)
        queue_protect = None
        if queue_need_pending > 0:
            queue_protect = max(0.0, opt_bp - queue_need_pending)

        action, qty, reason = decide_float_order(
            target_mv, sgov_qty, sgov_price, band=band,
            pending_buy_qty=pending_buy_qty, pending_sell_qty=pending_sell_qty,
            buy_capacity_usd=buy_capacity,
            queue_bp_protect_usd=queue_protect)

        log.info(f"[SGOV FLOAT] equity ${int(equity_v)} cap ${int(risk_cap)} "
                 f"target ${int(target_mv)} held ${int(sgov_mv)} band ${int(band)} "
                 f"-> {action}{' ' + str(qty) if qty else ''} ({reason})")
        target_qty = math.floor(target_mv / sgov_price) if sgov_price > 0 else 0
        monthly = target_mv * SGOV_YIELD_APY / 12.0
        # Dashboard snapshot parse compat ([SGOV] target/<$>, earning<$>/mo).
        log.info(f"[SGOV] target {target_qty} shares ${int(target_mv)} "
                 f"held {sgov_qty}x${sgov_price:.2f}=${int(sgov_mv)} "
                 f"earning ${monthly:.2f}/mo (v2.8 float model)")

        if action == "buy":
            order_fn(client, "buy", qty, logger_obj=log)
        elif action == "sell":
            order_fn(client, "sell", qty, logger_obj=log)
    except Exception as e:
        log.warning(f"SGOV float sync failed: {e}")
