"""Dividend calendar v2.4.1 - enhanced with OVERVIEW ExDividendDate for next dividend
Alpha Vantage DIVIDENDS + OVERVIEW + Finnhub fallback
"""
import os
import json
import time
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple

LOG_DIR = Path(__file__).parent.parent / "logs"
CACHE_FILE = LOG_DIR / "dividend_cache.json"
CACHE_TTL = 12*3600

def get_alpha_key():
    try:
        from config.credentials import ALPHA_VANTAGE_API_KEY
        return ALPHA_VANTAGE_API_KEY
    except Exception:
        return os.getenv("ALPHA_VANTAGE_API_KEY") or ""

def get_finnhub_key():
    try:
        from config.credentials import FINNHUB_API_KEY
        return FINNHUB_API_KEY
    except Exception:
        return os.getenv("FINNHUB_API_KEY") or ""

def fetch_dividends_alpha(symbol: str) -> List[Dict]:
    key = get_alpha_key()
    if not key:
        return []
    import requests
    # Try OVERVIEW first for next ex-div (faster)
    try:
        url = "https://www.alphavantage.co/query"
        params = {"function": "OVERVIEW", "symbol": symbol, "apikey": key}
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        ex = data.get("ExDividendDate") or data.get("DividendDate")
        if ex:
            try:
                # ExDividendDate is YYYY-MM-DD
                datetime.fromisoformat(ex)
                return [{"symbol": symbol.upper(), "exDate": ex, "amount": data.get("DividendPerShare"), "source": "OVERVIEW"}]
            except Exception:
                pass
    except Exception:
        pass

    # Fallback to DIVIDENDS endpoint
    try:
        url = "https://www.alphavantage.co/query"
        params = {"function": "DIVIDENDS", "symbol": symbol, "apikey": key}
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        dividends = data.get("data", []) if isinstance(data.get("data"), list) else []
        result = []
        for entry in dividends[:5]:
            ex = entry.get("ex_dividend_date") or entry.get("exDate")
            if ex:
                result.append({"symbol": symbol.upper(), "exDate": ex, "amount": entry.get("amount"), "source": "DIVIDENDS"})
        return result
    except Exception as e:
        # print(f"[DIVIDEND] Alpha {symbol} failed: {e}")
        return []

def fetch_dividends_finnhub(symbol: str, from_date: date, to_date: date) -> List[Dict]:
    key = get_finnhub_key()
    if not key:
        return []
    import requests
    url = "https://finnhub.io/api/v1/stock/dividend"
    params = {"symbol": symbol, "from": from_date.isoformat(), "to": to_date.isoformat(), "token": key}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 429:
            time.sleep(1)
            r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        result = []
        for entry in data:
            ex_ts = entry.get("exDate") or entry.get("date")
            if ex_ts:
                try:
                    if isinstance(ex_ts, int):
                        ex_d = datetime.fromtimestamp(ex_ts).date().isoformat()
                    else:
                        ex_d = str(ex_ts)[:10]
                    result.append({"symbol": symbol.upper(), "exDate": ex_d, "amount": entry.get("amount"), "source": "Finnhub"})
                except Exception:
                    pass
        return result
    except Exception:
        return []

def build_cache(symbols: List[str], days_ahead: int = 30) -> Dict[str, date]:
    today = date.today()
    future = today + timedelta(days=days_ahead)
    cache: Dict[str, date] = {}
    if CACHE_FILE.exists():
        try:
            raw = json.loads(CACHE_FILE.read_text())
            ts = raw.get("_timestamp", 0)
            if time.time() - ts < CACHE_TTL:
                for entry in raw.get("dividends", []):
                    sym = entry.get("symbol", "").upper()
                    if sym in set(s.upper() for s in symbols):
                        try:
                            d = datetime.fromisoformat(entry.get("exDate","")).date()
                            if (d - today).days >= -1:
                                cache[sym] = d
                        except Exception:
                            pass
                if cache:
                    print(f"[DIVIDEND] Cache hit {len(cache)} from {CACHE_FILE}")
                    return cache
        except Exception:
            pass

    dividend_map: Dict[str, date] = {}
    for sym in symbols[:15]:  # limit for rate
        divs = fetch_dividends_alpha(sym)
        if divs:
            for entry in divs:
                try:
                    ex = datetime.fromisoformat(entry["exDate"]).date()
                    if today <= ex <= future:
                        if sym.upper() not in dividend_map or ex < dividend_map[sym.upper()]:
                            dividend_map[sym.upper()] = ex
                except Exception:
                    continue
        else:
            divs_f = fetch_dividends_finnhub(sym, today, future)
            for entry in divs_f:
                try:
                    ex = datetime.fromisoformat(entry["exDate"]).date()
                    if today <= ex <= future:
                        if sym.upper() not in dividend_map or ex < dividend_map[sym.upper()]:
                            dividend_map[sym.upper()] = ex
                except Exception:
                    continue
        time.sleep(0.4)

    for k,v in cache.items():
        if k not in dividend_map:
            dividend_map[k] = v

    try:
        LOG_DIR.mkdir(exist_ok=True)
        CACHE_FILE.write_text(json.dumps({
            "_timestamp": time.time(),
            "_from": today.isoformat(),
            "_to": future.isoformat(),
            "dividends": [{"symbol": k, "exDate": v.isoformat()} for k,v in dividend_map.items()]
        }, indent=2))
    except Exception:
        pass

    return dividend_map

def is_dividend_risk(symbol: str, div_map: Dict[str, date], today: date = None, block_days: int = 2, dte: int = None, is_call: bool = False) -> Tuple[bool, str]:
    if today is None:
        today = date.today()
    sym = symbol.upper()
    if sym not in div_map:
        return (False, "")
    ex_date = div_map[sym]
    days_until = (ex_date - today).days
    if days_until < 0:
        return (False, "")
    if not is_call:
        if days_until <= 2:
            return (False, f"Ex-div in {days_until}d {ex_date} - puts low risk")
        return (False, "")
    if days_until == 0:
        return (True, f"Ex-div TODAY {ex_date} - block calls early assignment")
    if days_until <= block_days:
        return (True, f"Ex-div in {days_until}d {ex_date} within {block_days}d - block calls")
    if dte is not None and days_until <= dte:
        return (True, f"Ex-div {ex_date} in {days_until}d during DTE {dte} - block calls")
    return (False, "")

def get_dividend_risk_report(symbols: List[str], block_days: int = 2, days_ahead: int = 30, dte_default: int = None, is_call: bool = False) -> Dict:
    d_map = build_cache(symbols, days_ahead=days_ahead)
    today = date.today()
    report = {}
    for sym in symbols:
        up = sym.upper()
        if up in d_map:
            e_d = d_map[up]
            blocked, reason = is_dividend_risk(up, d_map, today, block_days, dte_default, is_call)
            report[up] = {"ex_date": e_d.isoformat(), "days_until": (e_d - today).days, "blocked": blocked, "reason": reason, "is_call": is_call}
        else:
            report[up] = {"ex_date": None, "days_until": None, "blocked": False, "reason": "", "is_call": is_call}
    return report
