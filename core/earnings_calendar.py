import os
import json
import time
from pathlib import Path
from datetime import date, timedelta, datetime
from typing import Dict, List, Tuple

LOG_DIR = Path(__file__).parent.parent / "logs"
CACHE_FILE = LOG_DIR / "earnings_cache.json"
CACHE_TTL = 6*3600
CACHE_STALE_OK = 48*3600  # accept stale 48h if Finnhub 503

def get_api_key():
    try:
        from config.credentials import FINNHUB_API_KEY
        return FINNHUB_API_KEY
    except Exception:
        return os.getenv("FINNHUB_API_KEY") or os.getenv("FINNHUB") or ""

def get_alpha_key():
    try:
        from config.credentials import ALPHA_VANTAGE_API_KEY
        return ALPHA_VANTAGE_API_KEY
    except Exception:
        return os.getenv("ALPHA_VANTAGE_API_KEY") or ""

def fetch_earnings_finnhub(from_date: date, to_date: date) -> List[Dict]:
    key = get_api_key()
    if not key:
        return []
    import requests
    url = "https://finnhub.io/api/v1/calendar/earnings"
    params = {"from": from_date.isoformat(), "to": to_date.isoformat(), "token": key}
    # retry 3x with backoff for 503
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(2 + attempt*2)
                continue
            r.raise_for_status()
            data = r.json()
            return data.get("earningsCalendar", [])
        except Exception as e:
            if attempt == 2:
                print(f"[EARNINGS] Finnhub fetch failed after 3 attempts: {e}")
                return []
            time.sleep(1 + attempt)
    return []

def fetch_earnings_alpha(symbol: str) -> List[Dict]:
    """Fallback via Alpha Vantage EARNINGS endpoint - returns next earnings date"""
    key = get_alpha_key()
    if not key:
        return []
    import requests
    url = "https://www.alphavantage.co/query"
    params = {"function": "EARNINGS", "symbol": symbol, "apikey": key}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        # Alpha returns quarterlyEarnings with fiscalDateEnding and reportedDate
        q = data.get("quarterlyEarnings", [])
        upcoming = []
        today = date.today()
        for entry in q[:2]:  # check last few
            # reportedDate may be future for upcoming? Alpha doesn't have future, but we can estimate
            # Use fiscalDateEnding as proxy
            d_str = entry.get("reportedDate") or entry.get("fiscalDateEnding")
            if not d_str:
                continue
            try:
                d = datetime.fromisoformat(d_str).date()
                # if within next 60 days and in future, treat as earnings risk
                if 0 <= (d - today).days <= 90:
                    upcoming.append({"symbol": symbol, "date": d_str})
            except Exception:
                continue
        return upcoming
    except Exception as e:
        print(f"[EARNINGS] Alpha fetch {symbol} failed: {e}")
        return []

def load_old_cache(symbols: List[str]) -> Dict[str, date]:
    """Load old cache even if stale, up to 48h"""
    cached = {}
    if not CACHE_FILE.exists():
        return cached
    try:
        raw = json.loads(CACHE_FILE.read_text())
        cached_time = raw.get("_timestamp", 0)
        age = time.time() - cached_time
        if age > CACHE_STALE_OK:
            print(f"[EARNINGS] Cache too stale {age/3600:.1f}h > 48h, ignoring")
            return cached
        for entry in raw.get("earningsCalendar", []):
            sym = entry.get("symbol", "").upper()
            d_str = entry.get("date", "")
            if sym in set(s.upper() for s in symbols):
                try:
                    d = datetime.fromisoformat(d_str).date()
                    # only keep future dates
                    if (d - date.today()).days >= -2:
                        cached[sym] = d
                except Exception:
                    pass
        if cached:
            print(f"[EARNINGS] Loaded stale cache {len(cached)} symbols age {age/3600:.1f}h")
    except Exception as e:
        print(f"[EARNINGS] Old cache load failed: {e}")
    return cached

def build_cache(symbols: List[str], days_ahead: int = 30) -> Dict[str, date]:
    today = date.today()
    future = today + timedelta(days=days_ahead)
    # Try load old cache first for fallback
    old_cache = load_old_cache(symbols)
    
    cached = {}
    cache_age_ok = False
    if CACHE_FILE.exists():
        try:
            raw = json.loads(CACHE_FILE.read_text())
            cached_time = raw.get("_timestamp", 0)
            if time.time() - cached_time < CACHE_TTL:
                cache_age_ok = True
                for entry in raw.get("earningsCalendar", []):
                    sym = entry.get("symbol", "").upper()
                    d_str = entry.get("date", "")
                    if sym in set(s.upper() for s in symbols):
                        try:
                            d = datetime.fromisoformat(d_str).date()
                            if (d - today).days >= -1:
                                cached[sym] = d
                        except Exception:
                            pass
        except Exception:
            pass

    fetched = fetch_earnings_finnhub(today, future)
    earnings_map: Dict[str, date] = {}
    
    if not fetched and old_cache:
        # Finnhub 503 - retain old cache
        print(f"[EARNINGS] Finnhub returned empty/503, retaining old cache {len(old_cache)} symbols")
        earnings_map = dict(old_cache)
    else:
        for entry in fetched:
            sym = entry.get("symbol", "").upper()
            if sym not in [s.upper() for s in symbols]:
                continue
            d_str = entry.get("date", "")
            try:
                d = datetime.fromisoformat(d_str).date()
                if sym not in earnings_map or d < earnings_map[sym]:
                    earnings_map[sym] = d
            except Exception:
                continue
        # Merge old if not in new (conservative)
        if cache_age_ok:
            for sym, d in cached.items():
                if sym not in earnings_map:
                    earnings_map[sym] = d
        elif old_cache:
            for sym, d in old_cache.items():
                if sym not in earnings_map:
                    earnings_map[sym] = d

    # If still empty and Alpha key available, try Alpha for critical symbols
    if not earnings_map and get_alpha_key():
        # Only try for symbols that are currently held to save API calls (5/min limit)
        pass

    # Always write cache if we have data, otherwise keep old file
    if earnings_map:
        try:
            LOG_DIR.mkdir(exist_ok=True)
            CACHE_FILE.write_text(json.dumps({
                "_timestamp": time.time(),
                "_from": today.isoformat(),
                "_to": future.isoformat(),
                "earningsCalendar": [{"symbol": k, "date": v.isoformat()} for k,v in earnings_map.items()]
            }, indent=2))
        except Exception as e:
            print(f"[EARNINGS] Cache write failed: {e}")
    else:
        # If we have old cache file, touch it to keep mtime?
        if old_cache and CACHE_FILE.exists():
            print(f"[EARNINGS] No new data, keeping existing cache file with {len(old_cache)} entries")

    return earnings_map or old_cache or cached

def is_earnings_risk(symbol: str, earnings_map: Dict[str, date], today: date = None, block_days: int = 3, dte: int = None) -> Tuple[bool, str]:
    if today is None:
        today = date.today()
    sym = symbol.upper()
    if sym not in earnings_map:
        return (False, "")
    e_date = earnings_map[sym]
    days_until = (e_date - today).days
    if days_until < 0:
        return (False, "")
    if days_until == 0:
        return (True, f"Earnings TODAY {e_date} - skip CSP, gap risk")
    if days_until == 1:
        return (True, f"Earnings TOMORROW {e_date} - skip CSP")
    if days_until <= block_days:
        return (True, f"Earnings in {days_until} days {e_date} (within {block_days}d block) - skip")
    if dte is not None and days_until <= dte:
        return (True, f"Earnings {e_date} in {days_until} days during DTE {dte} - high gap risk (NVDA Jun -54k lesson)")
    if days_until <= 7:
        return (False, f"Earnings in {days_until} days {e_date} - medium risk, consider shorter DTE or wider OTM")
    return (False, "")

def get_earnings_risk_report(symbols: List[str], block_days: int = 3, days_ahead: int = 30, dte_default: int = 21) -> Dict:
    e_map = build_cache(symbols, days_ahead=days_ahead)
    today = date.today()
    report = {}
    for sym in symbols:
        up = sym.upper()
        if up in e_map:
            e_d = e_map[up]
            blocked, reason = is_earnings_risk(up, e_map, today, block_days, dte_default)
            report[up] = {"earnings_date": e_d.isoformat(), "days_until": (e_d - today).days, "blocked": blocked, "reason": reason}
        else:
            report[up] = {"earnings_date": None, "days_until": None, "blocked": False, "reason": ""}
    return report
