"""
P/L Tracker v2.5.4 - Correct real P/L from Alpaca activities vs Optionable simplistic

Bug context:
Optionable tracker had closePrice=0 for every close -> profit = entry (sell premium) always.
Phantom P/L $568 vs real $52 realized when many trades closed at 50% profit (buy $0.50 for sell $1.00).

Real P/L formula:
  realized = Σ(sell_to_open qty*price*100) - Σ(buy_to_close qty*price*100) - fees
  where fees = commission per contract * qty * 2 (open+close) ; 0 paper, 0.65 live
  Unrealized = Σ((avg_entry - current_price)*100*qty - commission) for open short puts

This module computes correctly from Alpaca orders + positions, separate from Optionable's calc.

Provides:
- get_real_pnl(client) -> dict {realized, unrealized, fees, total, breakdown, discrepancy}
- get_optionable_pnl() -> fetch from Optionable API for comparison
- compare_pnl() -> logs discrepancy
"""

import logging
import os
import datetime
from typing import Dict, List, Optional, Tuple
import requests

logger = logging.getLogger("strategy.pnl_tracker")

OPTIONABLE_URL = os.getenv("OPTIONABLE_URL", "http://localhost:8096")
TIMEOUT = 8

def _commission_per_contract() -> float:
    try:
        from config.credentials import IS_PAPER
        return 0.0 if IS_PAPER else 0.65
    except Exception:
        return 0.0 if os.getenv("ALPACA_PAPER","true").lower() in ("true","1") else 0.65

def _parse_occ_simple(occ: str):
    """Minimal OCC parse for P/L grouping"""
    import re
    m = re.match(r'^([A-Z]+)(\d{6})([PC])(\d{8})$', occ.strip())
    if not m:
        return None
    return m.group(1)  # underlying

def _fetch_all_closed_orders(client, limit_pages=10) -> List:
    """Fetch closed orders with pagination"""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        all_orders = []
        next_token = None
        for _ in range(limit_pages):
            kwargs = {"status": QueryOrderStatus.CLOSED, "limit": 100}
            if next_token:
                kwargs["page_token"] = next_token
            req = GetOrdersRequest(**kwargs)
            resp = client.trade_client.get_orders(filter=req)
            chunk = resp if isinstance(resp, list) else getattr(resp, 'data', []) or list(resp)
            if not chunk:
                break
            all_orders.extend(chunk)
            next_token = getattr(resp, 'next_page_token', None) if not isinstance(resp, list) else None
            if not next_token:
                break
        return all_orders
    except Exception as e:
        logger.debug(f"_fetch_all_closed_orders failed: {e}")
        return []

def _compute_realized_from_orders(client) -> Tuple[float, float, List[Dict]]:
    """Compute realized P/L from closed orders"""
    comm_per = _commission_per_contract()
    orders = _fetch_all_closed_orders(client)
    # Group by OCC
    occ_data: Dict[str, Dict] = {}
    for o in orders:
        try:
            sym = getattr(o, 'symbol', None)
            if not sym:
                continue
            # Filter options only (OCC length >=15)
            if len(sym) < 10:
                continue
            # Validate OCC pattern roughly: ends with 8 digits
            if not sym[-8:].isdigit():
                continue
            side = str(getattr(o, 'side', '')).lower()
            qty_f = float(getattr(o, 'filled_qty', 0) or getattr(o, 'qty',0) or 0)
            price = getattr(o, 'filled_avg_price', None) or getattr(o, 'filled_price', None)
            if price is None or qty_f==0:
                continue
            price_f = float(price)
            if sym not in occ_data:
                occ_data[sym] = {"sell_qty":0.0, "buy_qty":0.0, "sell_mv":0.0, "buy_mv":0.0, "sells":[], "buys":[]}
            mv = qty_f * price_f * 100.0
            if 'sell' in side:
                occ_data[sym]["sell_qty"] += qty_f
                occ_data[sym]["sell_mv"] += mv
                occ_data[sym]["sells"].append({"qty":qty_f, "price":price_f, "mv":mv})
            elif 'buy' in side:
                occ_data[sym]["buy_qty"] += qty_f
                occ_data[sym]["buy_mv"] += mv
                occ_data[sym]["buys"].append({"qty":qty_f, "price":price_f, "mv":mv})
        except Exception:
            continue

    realized = 0.0
    fees_total = 0.0
    breakdown = []
    for occ, d in occ_data.items():
        # Only consider closed where buy_qty >= sell_qty or both present (wheel closes short with buy)
        # Simplistic: if both sell and buy exist, profit = sell_mv - buy_mv - fees
        if d["sell_qty"]>0 and d["buy_qty"]>0:
            # Fees: commission per contract * total contracts traded (sell+buy for realized? Actually open+close)
            # For realistic: each round-trip 2*comm, but our mv doesn't include commission; subtract
            qty_closed = min(d["sell_qty"], d["buy_qty"])
            fees = comm_per * qty_closed * 2.0  # open+close per closed qty
            pl = d["sell_mv"] - d["buy_mv"] - fees
            # If multiple sells/buys, pro-rate to closed qty if mismatched
            if d["sell_qty"] != d["buy_qty"]:
                # e.g., rolled? Assume sell_qty > buy_qty partially open, only buy portion realized
                # Simplified: if sell_qty>buy_qty, realized = (sell_mv/sell_qty*buy_qty) - buy_mv - fees
                # We'll compute weighted
                avg_sell = d["sell_mv"]/d["sell_qty"] if d["sell_qty"] else 0
                realized_mv = avg_sell * d["buy_qty"]
                pl = realized_mv - d["buy_mv"] - fees
            realized += pl
            fees_total += fees
            breakdown.append({
                "occ": occ,
                "underlying": _parse_occ_simple(occ) or occ[:5],
                "sell_qty": d["sell_qty"],
                "buy_qty": d["buy_qty"],
                "sell_mv": d["sell_mv"],
                "buy_mv": d["buy_mv"],
                "fees": fees,
                "realized_pl": pl
            })

    return realized, fees_total, breakdown

def _compute_unrealized(client) -> Tuple[float, List[Dict]]:
    """Compute unrealized P/L from open positions"""
    comm_per = _commission_per_contract()
    try:
        positions = client.get_positions()
        unrealized = 0.0
        breakdown = []
        for p in positions:
            ac = str(getattr(p, "asset_class","")).upper()
            if "OPTION" not in ac:
                continue
            try:
                qty = float(getattr(p, "qty",0) or 0)  # negative for short
                qty_abs = abs(qty)
                avg_entry = float(getattr(p, "avg_entry_price",0) or 0)
                cur = float(getattr(p, "current_price",0) or getattr(p, "market_value",0)/qty_abs/100 if qty_abs else 0)
                if qty < 0:  # short option
                    gross = (avg_entry - cur) * 100.0 * qty_abs
                    fees = comm_per * qty_abs  # only close fee remaining? Actually open already paid, but we account both?
                    # For unrealized, fees = close fee only? We'll use 1x
                    net = gross - comm_per*qty_abs
                    unrealized += net
                    breakdown.append({"symbol": getattr(p,"symbol",""), "qty":qty, "avg":avg_entry, "cur":cur, "unreal":net, "gross":gross})
            except Exception:
                continue
        return unrealized, breakdown
    except Exception as e:
        logger.debug(f"_compute_unrealized failed: {e}")
        return 0.0, []

def get_optionable_pnl(account_id: Optional[int]=None) -> Dict:
    """Fetch Optionable P/L for comparison - fetch all trades via API"""
    result = {"realized":0.0, "unrealized":0.0, "total":0.0, "fees":0.0, "count_closed":0}
    try:
        if account_id is None:
            try:
                # reuse helper from optionable_sync if available
                from core.optionable_sync import get_default_account_id
                account_id = get_default_account_id()
            except Exception:
                account_id = 1
        r = requests.get(f"{OPTIONABLE_URL}/api/trades?accountId={account_id}", timeout=TIMEOUT)
        if r.status_code != 200:
            return result
        trades = r.json().get('data') or []
        for tr in trades:
            try:
                entry = float(tr.get('entryPrice',0) or 0)
                close = float(tr.get('closePrice',0) or 0)
                qty = int(tr.get('quantity',1) or 1)
                comm = float(tr.get('commission',0) or 0)
                status = tr.get('status','Open')
                if status in ('Closed','Expired','Assigned'):
                    if status == 'Assigned':
                        pnl = entry*100*qty - comm
                    else:
                        pnl = (entry - close)*100*qty - comm
                    result["realized"] += pnl
                    result["fees"] += comm
                    result["count_closed"] += 1
                elif status == 'Open':
                    # Optionable unrealized? If it has current price? We estimate using closePrice=0? Actually open trades have close 0
                    # We'll not compute unrealized from Optionable, use 0
                    pass
            except Exception:
                continue
        result["total"] = result["realized"] + result["unrealized"]
        return result
    except Exception as e:
        logger.debug(f"get_optionable_pnl failed: {e}")
        return result

def get_real_pnl(client) -> Dict:
    """
    Main function: returns dict with realized, unrealized, fees, total, breakdown, optionable comparison, discrepancy
    
    Returns:
    {
        "realized": float,  # real realized from Alpaca fills
        "unrealized": float, # unrealized from open positions
        "fees": float,       # total fees estimated
        "total": float,      # realized+unrealized
        "realized_breakdown": [...],
        "unrealized_breakdown": [...],
        "optionable_pnl": {...},
        "discrepancy": float, # optionable_realized - real_realized
        "discrepancy_pct": float,
        "timestamp": iso,
        "is_paper": bool
    }
    """
    # Realized from Alpaca
    realized, fees_realized, breakdown_realized = _compute_realized_from_orders(client)
    unrealized, breakdown_unrealized = _compute_unrealized(client)

    # Fees total = realized fees + unrealized close fees estimate
    comm_per = _commission_per_contract()
    fees_unrealized = sum(comm_per * abs(float(b.get("qty",0))) for b in breakdown_unrealized)  # simplified
    fees_total = fees_realized + fees_unrealized

    total = realized + unrealized

    # Optionable for comparison
    optionable = get_optionable_pnl()

    discrepancy = optionable.get("realized",0.0) - realized
    discrepancy_pct = (discrepancy / realized * 100) if realized !=0 else 0.0

    # Log critical if discrepancy large (phantom bug)
    try:
        from config.params import PNL_DISCREPANCY_THRESHOLD
        thresh = PNL_DISCREPANCY_THRESHOLD
    except Exception:
        thresh = 50.0

    if abs(discrepancy) > thresh:
        logger.warning(
            f"[P/L TRACKER] Discrepancy ALERT real ${realized:.2f} vs optionable ${optionable.get('realized',0):.2f} "
            f"diff ${discrepancy:.2f} ({discrepancy_pct:.1f}%) > ${thresh} threshold - "
            f"likely closePrice=0 phantom bug {len(breakdown_realized)} trades"
        )
    else:
        logger.info(
            f"[P/L TRACKER] Real P/L realized ${realized:.2f} unreal ${unrealized:.2f} fees ${fees_total:.2f} total ${total:.2f} "
            f"| Optionable realized ${optionable.get('realized',0):.2f} | discrepancy ${discrepancy:.2f}"
        )

    is_paper = True
    try:
        from config.credentials import IS_PAPER
        is_paper = bool(IS_PAPER)
    except Exception:
        pass

    return {
        "realized": round(realized,2),
        "unrealized": round(unrealized,2),
        "fees": round(fees_total,2),
        "fees_realized": round(fees_realized,2),
        "fees_unrealized": round(fees_unrealized,2),
        "total": round(total,2),
        "realized_breakdown": breakdown_realized,
        "unrealized_breakdown": breakdown_unrealized,
        "optionable_pnl": optionable,
        "optionable_realized": optionable.get("realized",0.0),
        "discrepancy": round(discrepancy,2),
        "discrepancy_pct": round(discrepancy_pct,2),
        "timestamp": datetime.datetime.now().isoformat(),
        "is_paper": is_paper,
        "notes": "v2.5.4 fixes closePrice=0 phantom - real = sell - buy - fees, optionable simplistic"
    }

# Compatibility wrapper for strategy_logger 30 factors
def get_pnl_summary_for_logging(client) -> Dict:
    """Returns flattened dict for strategy_logger 30-factor inclusion"""
    data = get_real_pnl(client)
    return {
        "real_pnl_realized": data["realized"],
        "real_pnl_unrealized": data["unrealized"],
        "real_pnl_fees": data["fees"],
        "real_pnl_total": data["total"],
        "optionable_pnl": data["optionable_realized"],
        "pnl_discrepancy": data["discrepancy"],
        "pnl_discrepancy_pct": data["discrepancy_pct"],
        "real_pnl_trade_count": len(data["realized_breakdown"]),
    }
