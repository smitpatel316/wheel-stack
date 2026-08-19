"""
Wheeler ↔ options-wheel bridge
Pushes trades executed by options-wheel into Wheeler tracker (localhost:8096)
Uses Wheeler REST APIs: POST /api/options, /api/symbols/{symbol}

Wheeler DB convention (from wheel_strategy_example.sql):
- options.premium = per-share price, e.g. 0.80, not total. contracts separate.
- premium * contracts * 100 = total income
- Idempotent via UNIQUE index (symbol,type,opened,strike,expiration,premium,contracts)

Safe for paper: never logs API keys.
"""
import datetime
import logging
import os
import re
from pathlib import Path
from typing import Optional, Tuple

import requests

logger = logging.getLogger("strategy.wheeler_sync")

WHEELER_URL = os.getenv("WHEELER_URL", "http://localhost:8096")
TIMEOUT = 5

def wheeler_alive() -> bool:
    try:
        r = requests.get(f"{WHEELER_URL}/api/allocation-data", timeout=TIMEOUT)
        return r.status_code == 200
    except Exception as e:
        logger.debug(f"Wheeler not reachable at {WHEELER_URL}: {e}")
        return False

def _parse_occ(occ_symbol: str) -> Optional[Tuple[str, str, str, float, str]]:
    """
    Parse OCC symbol like AAPL260116P00308000, F260116P00015000, SPY260116C00740000
    Returns: (underlying, exp_date YYYY-MM-DD, Put/Call, strike, YYMMDD raw)
    """
    # Alpaca OCC: underlying can be 1-6 chars, then 6 YYMMDD, then P/C, then 8 strike*1000
    # Use regex from core.utils but with expanded underlying group
    m = re.match(r'^([A-Z]+)(\d{6})([PC])(\d{8})$', occ_symbol.strip())
    if not m:
        # try with spaces or extended underlying like BRK.B? Strip dot
        m = re.match(r'^([A-Z\.\/]+)(\d{6})([PC])(\d{8})$', occ_symbol.strip().replace(" ", ""))
    if not m:
        logger.warning(f"Failed to parse OCC {occ_symbol}")
        return None
    underlying_raw = m.group(1).strip()
    yymmdd = m.group(2)
    pc = m.group(3)
    strike_raw = m.group(4)
    # date
    yy = int(yymmdd[:2])
    year = 2000 + yy if yy < 70 else 1900 + yy
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    try:
        exp_date = datetime.date(year, month, day).isoformat()
    except ValueError:
        logger.warning(f"Invalid date in OCC {occ_symbol} -> {yymmdd}")
        return None
    opt_type = "Put" if pc == "P" else "Call"
    strike = int(strike_raw) / 1000.0
    return underlying_raw, exp_date, opt_type, strike, yymmdd

def ensure_symbol(underlying: str, price_hint: float = 0):
    try:
        # PUT creates if missing; if exists updates price only if provided
        payload = {"price": float(price_hint)} if price_hint else {}
        r = requests.put(f"{WHEELER_URL}/api/symbols/{underlying}", json=payload, timeout=TIMEOUT)
        logger.debug(f"Wheeler symbol {underlying} upsert {r.status_code}")
        return True
    except Exception as e:
        logger.debug(f"Wheeler symbol upsert failed {underlying}: {e}")
        return False

def push_option_to_wheeler(
    alpaca_occ_symbol: str,
    bid_per_share: float,
    contracts: int = 1,
    opened_date: Optional[str] = None,
) -> bool:
    """
    Push a sold option (CSP or CC) into Wheeler tracker.
    bid_per_share: e.g. 0.85 = $0.85 per share -> Wheeler stores as premium = 0.85
    Idempotent: duplicate UNIQUE will be treated as success.
    """
    if not wheeler_alive():
        return False

    parsed = _parse_occ(alpaca_occ_symbol)
    if not parsed:
        return False
    underlying, exp_date, opt_type, strike, _ = parsed
    if opened_date is None:
        opened_date = datetime.date.today().isoformat()

    ensure_symbol(underlying, price_hint=strike)

    # Wheeler expects per-share premium, same as bid_price from snapshot
    payload = {
        "symbol": underlying,
        "type": opt_type,
        "opened": opened_date,
        "strike": strike,
        "expiration": exp_date,
        "premium": float(bid_per_share),
        "contracts": int(contracts),
        "commission": 0.65 * int(contracts),
    }
    try:
        r = requests.post(f"{WHEELER_URL}/api/options", json=payload, timeout=TIMEOUT)
        if r.status_code in (200, 201):
            logger.info(f"Wheeler: logged {opt_type} {underlying} ${strike} exp {exp_date} premium ${bid_per_share:.2f} x{contracts}")
            return True
        txt = r.text[:500]
        # Duplicate unique index is okay
        if "UNIQUE" in txt or "unique" in txt.lower() or r.status_code in (409, 500) and "UNIQUE" in txt.upper():
            logger.info(f"Wheeler: {underlying} {opt_type} ${strike} {exp_date} already exists (idempotent)")
            return True
        # 500 can also be duplicate
        if r.status_code == 500 and ("constraint" in txt.lower() or "exists" in txt.lower()):
            logger.info(f"Wheeler: already exists {underlying} {opt_type} {exp_date} (idempotent)")
            return True
        logger.warning(f"Wheeler POST failed {r.status_code}: {txt}")
        return False
    except Exception as e:
        logger.warning(f"Wheeler POST exception: {e}")
        return False

def sync_alpaca_equity_positions_to_wheeler(client):
    """Optional sync: push equity positions held in Alpaca paper into Wheeler long_positions - idempotent"""
    if not wheeler_alive():
        return
    try:
        import subprocess
        positions = client.get_positions()
        for p in positions:
            ac = str(getattr(p, "asset_class", "")).upper()
            if "OPTION" in ac:
                continue
            sym = getattr(p, "symbol", None)
            if not sym:
                continue
            try:
                qty = int(float(getattr(p, "qty", 0)))
            except Exception as e:
                logger.debug("[SWALLOWED] Wheeler equity sync: qty parse failed for %s: %r", sym, e)
                continue
            if qty <= 0:
                continue
            try:
                avg = float(getattr(p, "avg_entry_price", 0))
                cur_price = float(getattr(p, "current_price", avg) or avg)
                opened = datetime.date.today().isoformat()
                ensure_symbol(sym, cur_price)
                # Idempotent: DELETE then POST to avoid doubling on each cron
                try:
                    subprocess.run(
                        f"sg docker -c \"docker exec wheeler sqlite3 /app/data/wheeler.db \\\"DELETE FROM long_positions WHERE symbol='{sym}';\\\"\"",
                        shell=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                except Exception as e:
                    logger.warning("[SWALLOWED] Wheeler idempotent DELETE for %s failed, POST may double-count: %r", sym, e)
                    pass
                payload = {
                    "symbol": sym,
                    "shares": qty,
                    "buy_price": avg,
                    "opened": opened,
                }
                r = requests.post(f"{WHEELER_URL}/api/long-positions", json=payload, timeout=TIMEOUT)
                if r.status_code in (200, 201):
                    logger.info(f"Wheeler: synced long {sym} {qty}x${avg}")
            except Exception as e:
                logger.debug(f"Wheeler long sync {sym} failed: {e}")
    except Exception as e:
        logger.warning(f"sync_alpaca_equity_positions failed: {e}")
