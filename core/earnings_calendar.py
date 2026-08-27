import os
import io
import csv
import json
import logging
import time
from pathlib import Path
from datetime import date, timedelta, datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(f"strategy.{__name__}")

LOG_DIR = Path(__file__).parent.parent / "logs"
CACHE_FILE = LOG_DIR / "earnings_cache.json"
CACHE_TTL = 6*3600
CACHE_STALE_OK = 48*3600  # accept stale 48h if Finnhub 503

# Last-good snapshot (Pi migration, 2026-08-27): a copy of the most recent
# non-empty earnings cache, written atomically alongside every cache write.
# When the primary cache is missing OR older than CACHE_STALE_OK (e.g. a
# multi-day outage), load_old_cache falls back to this snapshot before
# giving up — entries self-filter by date, so an old-but-valid snapshot is
# strictly better than nothing. No TTL; age is logged loudly on use.
LAST_GOOD_FILE = Path(__file__).parent.parent / "state" / "earnings-last-good.json"


def _load_last_good(symbols: List[str]) -> Dict[str, date]:
    """Load the last-good snapshot regardless of age. Returns {} if absent."""
    if not LAST_GOOD_FILE.exists():
        return {}
    try:
        raw = json.loads(LAST_GOOD_FILE.read_text())
        age_h = (time.time() - float(raw.get("_timestamp", 0))) / 3600.0
        out: Dict[str, date] = {}
        for entry in raw.get("earningsCalendar", []):
            sym = entry.get("symbol", "").upper()
            d_str = entry.get("date", "")
            if sym in set(s.upper() for s in symbols):
                try:
                    d = datetime.fromisoformat(d_str).date()
                    if (d - date.today()).days >= -2:  # self-filter past dates
                        out[sym] = d
                except Exception as e:
                    logger.debug("[SWALLOWED] parsing last-good earnings date for %s: %r", sym, e)
        if out:
            logger.warning("[EARNINGS] Using LAST-GOOD snapshot (%d symbols, age %.1fh) - "
                           "primary cache missing/too stale", len(out), age_h)
            print(f"[EARNINGS] Using last-good snapshot {len(out)} symbols age {age_h:.1f}h (cache missing/stale)")
        return out
    except Exception as e:
        logger.debug("[SWALLOWED] loading last-good earnings snapshot %s: %r", LAST_GOOD_FILE, e)
        return {}

def get_api_key():
    try:
        from config.credentials import FINNHUB_API_KEY
        return FINNHUB_API_KEY
    except Exception as e:
        logger.debug("[SWALLOWED] loading FINNHUB_API_KEY from config.credentials, falling back to env: %r", e)
        return os.getenv("FINNHUB_API_KEY") or os.getenv("FINNHUB") or ""

def get_alpha_key():
    try:
        from config.credentials import ALPHA_VANTAGE_API_KEY
        return ALPHA_VANTAGE_API_KEY
    except Exception as e:
        logger.debug("[SWALLOWED] loading ALPHA_VANTAGE_API_KEY from config.credentials, falling back to env: %r", e)
        return os.getenv("ALPHA_VANTAGE_API_KEY") or ""

def fetch_earnings_finnhub(from_date: date, to_date: date) -> Optional[List[Dict]]:
    """Returns the calendar list, or None on failure (distinct from a
    genuinely empty calendar, which returns []). Callers rely on this to
    decide whether to fall back / retain stale data."""
    key = get_api_key()
    if not key:
        print("[EARNINGS] No Finnhub API key - earnings feed unavailable")
        return None
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
            # Never log %r of the exception here - HTTPError includes the request URL with the Finnhub API token
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            logger.warning("[SWALLOWED] Finnhub earnings calendar fetch failed (attempt %d/3, HTTP %s): %s", attempt + 1, status, type(e).__name__)
            if attempt == 2:
                print(f"[EARNINGS] Finnhub fetch failed after 3 attempts: {type(e).__name__} HTTP {status}")
                return None
            time.sleep(1 + attempt)
    print("[EARNINGS] Finnhub rate-limited (429) on all 3 attempts")
    return None


def fetch_earnings_calendar_alpha(symbols: List[str]) -> Dict[str, date]:
    """Fallback via Alpha Vantage EARNINGS_CALENDAR (CSV, horizon=3month).

    Unlike the EARNINGS endpoint (historical only), EARNINGS_CALENDAR
    includes FUTURE report dates, so it can actually serve as the fallback
    the old fetch_earnings_alpha() never could be. One API call for the
    whole watchlist. Returns {symbol: next_report_date}.
    """
    key = get_alpha_key()
    if not key:
        print("[EARNINGS] No Alpha Vantage key - calendar fallback unavailable")
        return {}
    import requests
    url = "https://www.alphavantage.co/query"
    params = {"function": "EARNINGS_CALENDAR", "horizon": "3month", "apikey": key}
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        want = set(s.upper() for s in symbols)
        today = date.today()
        out: Dict[str, date] = {}
        for row in csv.DictReader(io.StringIO(r.text)):
            sym = (row.get("symbol") or "").upper()
            if sym not in want:
                continue
            d_str = (row.get("reportDate") or "").strip()
            try:
                d = datetime.fromisoformat(d_str).date()
            except Exception as e:
                logger.debug("[SWALLOWED] parsing Alpha EARNINGS_CALENDAR reportDate %r for %s: %r", d_str, sym, e)
                continue
            if d < today:
                continue
            if sym not in out or d < out[sym]:
                out[sym] = d
        print(f"[EARNINGS] Alpha EARNINGS_CALENDAR fallback: {len(out)} watchlist dates")
        return out
    except Exception as e:
        logger.warning("[SWALLOWED] Alpha EARNINGS_CALENDAR fallback fetch failed: %r", e)
        print(f"[EARNINGS] Alpha EARNINGS_CALENDAR fallback failed: {e}")
        return {}

def load_old_cache(symbols: List[str]) -> Dict[str, date]:
    """Load old cache even if stale, up to 48h; then the last-good snapshot."""
    cached = {}
    if not CACHE_FILE.exists():
        return _load_last_good(symbols)
    try:
        raw = json.loads(CACHE_FILE.read_text())
        cached_time = raw.get("_timestamp", 0)
        age = time.time() - cached_time
        if age > CACHE_STALE_OK:
            print(f"[EARNINGS] Cache too stale {age/3600:.1f}h > 48h, ignoring")
            return _load_last_good(symbols)
        for entry in raw.get("earningsCalendar", []):
            sym = entry.get("symbol", "").upper()
            d_str = entry.get("date", "")
            if sym in set(s.upper() for s in symbols):
                try:
                    d = datetime.fromisoformat(d_str).date()
                    # only keep future dates
                    if (d - date.today()).days >= -2:
                        cached[sym] = d
                except Exception as e:
                    logger.debug("[SWALLOWED] parsing stale-cache earnings date for %s: %r", sym, e)
                    pass
        if cached:
            print(f"[EARNINGS] Loaded stale cache {len(cached)} symbols age {age/3600:.1f}h")
    except Exception as e:
        logger.debug("[SWALLOWED] loading stale earnings cache %s: %r", CACHE_FILE, e)
        print(f"[EARNINGS] Old cache load failed: {e}")
    return cached if cached else _load_last_good(symbols)

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
                        except Exception as e:
                            logger.debug("[SWALLOWED] parsing fresh-cache earnings date for %s: %r", sym, e)
                            pass
        except Exception as e:
            logger.debug("[SWALLOWED] loading earnings cache %s, will refetch: %r", CACHE_FILE, e)
            pass

    fetched = fetch_earnings_finnhub(today, future)
    earnings_map: Dict[str, date] = {}

    if fetched is None:
        # Finnhub FAILED (vs. genuinely empty calendar): retain stale cache,
        # and if there is nothing usable left, fall back to Alpha's
        # EARNINGS_CALENDAR (the one Alpha endpoint with future dates).
        if old_cache:
            print(f"[EARNINGS] Finnhub failed, retaining stale cache {len(old_cache)} symbols")
            earnings_map = dict(old_cache)
        elif cached:
            print(f"[EARNINGS] Finnhub failed, retaining fresh cache {len(cached)} symbols")
            earnings_map = dict(cached)
        else:
            earnings_map = fetch_earnings_calendar_alpha(symbols)
        if not earnings_map:
            print(f"[EARNINGS] WARNING: NO earnings data from Finnhub, cache, or Alpha - "
                  f"earnings blocking is DEGRADED for this run ({len(symbols)} symbols)")
    elif not fetched:
        # Successful fetch, empty calendar (off-season). Keep stale entries.
        if old_cache:
            print(f"[EARNINGS] Finnhub empty, retaining old cache {len(old_cache)} symbols")
            earnings_map = dict(old_cache)
        else:
            earnings_map = dict(cached)
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
            except Exception as e:
                logger.debug("[SWALLOWED] parsing Finnhub earnings date %r for %s: %r", d_str, sym, e)
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

    # Always write cache if we have data, otherwise keep old file
    if earnings_map:
        cache_doc = {
            "_timestamp": time.time(),
            "_from": today.isoformat(),
            "_to": future.isoformat(),
            "earningsCalendar": [{"symbol": k, "date": v.isoformat()} for k,v in earnings_map.items()]
        }
        try:
            LOG_DIR.mkdir(exist_ok=True)
            CACHE_FILE.write_text(json.dumps(cache_doc, indent=2))
        except Exception as e:
            logger.debug("[SWALLOWED] writing earnings cache %s: %r", CACHE_FILE, e)
            print(f"[EARNINGS] Cache write failed: {e}")
        # Last-good snapshot (atomic tmp+rename): survives >48h outages.
        try:
            LAST_GOOD_FILE.parent.mkdir(parents=True, exist_ok=True)
            _tmp = LAST_GOOD_FILE.with_suffix(".tmp")
            _tmp.write_text(json.dumps(cache_doc, indent=2))
            os.replace(_tmp, LAST_GOOD_FILE)
        except Exception as e:
            logger.debug("[SWALLOWED] writing earnings last-good snapshot %s: %r", LAST_GOOD_FILE, e)
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
