"""Volatility & IV Rank v2.4 - Alpha Vantage TIME_SERIES + Alpaca realized vol
No true IV history via API, so we proxy:
- Realized vol 20d vs 252d percentile = RV Rank (proxy for IV Rank)
- Alpha RSI/MACD optional
- Adapt delta: high VIX/IVRank -> 0.20, low -> 0.30 balanced per Sophie
"""
import os
import json
import logging
import time
import math
from pathlib import Path
from datetime import date, timedelta, datetime
from typing import Dict, List, Tuple

from core.data_fallbacks import fetch_daily_bars_alpaca

log = logging.getLogger(__name__)

LOG_DIR = Path(__file__).parent.parent / "logs"
CACHE_FILE = LOG_DIR / "volatility_cache.json"
CACHE_TTL = 4*3600

def get_alpha_key():
    try:
        from config.credentials import ALPHA_VANTAGE_API_KEY
        return ALPHA_VANTAGE_API_KEY
    except Exception as e:
        log.debug("[SWALLOWED] config Alpha Vantage key import failed, falling back to env: %r", e)
        return os.getenv("ALPHA_VANTAGE_API_KEY") or ""

def fetch_daily_alpha(symbol: str, days: int = 300) -> List[float]:
    key = get_alpha_key()
    if not key:
        return []
    import requests
    url = "https://www.alphavantage.co/query"
    params = {"function": "TIME_SERIES_DAILY", "symbol": symbol, "apikey": key, "outputsize": "full"}
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        ts = data.get("Time Series (Daily)", {})
        if "Note" in data or "Information" in data:
            time.sleep(2)
            return []
        closes = []
        for d_str in sorted(ts.keys(), reverse=True)[:days]:
            try:
                c = float(ts[d_str]["4. close"])
                closes.append(c)
            except Exception as e:
                log.debug("[SWALLOWED] Alpha daily close parse failed for %s %s: %r", symbol, d_str, e)
                continue
        closes.reverse()  # oldest first
        return closes
    except Exception as e:
        msg = repr(e).replace(key, "***") if key else repr(e)
        log.debug("[SWALLOWED] Alpha daily fetch failed for %s: %s", symbol, msg)
        print(f"[VOL] Alpha {symbol} daily failed: {msg}")
        return []

def realized_vol(closes: List[float], window: int = 20) -> float:
    """Annualized realized vol from closes"""
    if len(closes) < window+1:
        return 0.0
    rets = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            rets.append(math.log(closes[i]/closes[i-1]))
    if len(rets) < window:
        return 0.0
    # last window
    recent = rets[-window:]
    import statistics
    if len(recent) < 2:
        return 0.0
    std = statistics.stdev(recent)
    return std * math.sqrt(252) * 100  # % annualized

def compute_rv_rank(closes: List[float]) -> Tuple[float, float, float]:
    """Compute 20d RV, 252d RV, and RV Rank percentile (0-100)"""
    if len(closes) < 100:
        return (0.0, 0.0, 50.0)
    rv_20 = realized_vol(closes, 20)
    rv_60 = realized_vol(closes, 60)
    # Build history of 20d RVs over last year
    rvs = []
    for i in range(60, len(closes), 5):  # every 5 days to save compute
        slice_closes = closes[:i]
        rv = realized_vol(slice_closes, 20)
        if rv > 0:
            rvs.append(rv)
    if not rvs or rv_20 == 0:
        return (rv_20, rv_60, 50.0)
    # percentile
    sorted_rvs = sorted(rvs)
    rank = sum(1 for x in sorted_rvs if x <= rv_20) / len(sorted_rvs) * 100
    return (rv_20, rv_60, rank)

def build_cache(symbols: List[str]) -> Dict[str, Dict]:
    stale: Dict[str, Dict] = {}  # any-age on-disk real entries, fallback on fetch failure
    if CACHE_FILE.exists():
        try:
            raw = json.loads(CACHE_FILE.read_text())
            ts = raw.get("_timestamp", 0)
            data = {e["symbol"]: e for e in raw.get("volatility", [])}
            stale = {s: e for s, e in data.items() if e.get("source") in ("alpha_daily", "alpaca_bars")}
            if time.time() - ts < CACHE_TTL:
                # return if all symbols present
                if all(s.upper() in data for s in symbols[:5]):
                    print(f"[VOL] Cache hit {len(data)}")
                    return data
        except Exception as e:
            log.debug("[SWALLOWED] volatility cache read failed, rebuilding: %r", e)
            pass

    vol_map: Dict[str, Dict] = {}
    fresh = 0
    fallback_used = 0
    # Only fetch for few symbols per run due to rate limit (TIME_SERIES_DAILY is heavy)
    for sym in symbols[:8]:
        closes = fetch_daily_alpha(sym)
        source = "alpha_daily"
        if not closes:
            bars = fetch_daily_bars_alpaca(sym, days=450)  # ~300 trading days
            if bars:
                closes = [b["close"] for b in bars]
                source = "alpaca_bars"
                fallback_used += 1
                print(f"[VOL] {sym.upper()} via alpaca-bars-fallback ({len(closes)} bars)")
                log.info("[VOL] %s daily closes served by alpaca-bars-fallback", sym.upper())
        if closes:
            rv20, rv60, rv_rank = compute_rv_rank(closes)
            vol_map[sym.upper()] = {
                "symbol": sym.upper(),
                "rv_20d": round(rv20, 2),
                "rv_60d": round(rv60, 2),
                "iv_rank_proxy": round(rv_rank, 1),  # proxy for IV Rank
                "closes_count": len(closes),
                "source": source
            }
            fresh += 1
        elif sym.upper() in stale:
            # keep last real reading instead of a synthetic default
            vol_map[sym.upper()] = stale[sym.upper()]
        else:
            vol_map[sym.upper()] = {
                "symbol": sym.upper(),
                "rv_20d": 20.0,
                "rv_60d": 18.0,
                "iv_rank_proxy": 50.0,
                "source": "default"
            }
        time.sleep(0.6)

    if fresh == 0:
        n_stale = sum(1 for v in vol_map.values() if v.get("source") in ("alpha_daily", "alpaca_bars"))
        print(f"[VOL] All Alpha fetches failed - serving stale/default vol data ({n_stale} stale real, rest default IVR=50); IV screen degraded")
        log.warning("[VOL] all Alpha daily fetches failed; vol map is stale/default only")
    elif fallback_used:
        print(f"[VOL] {fallback_used}/{min(len(symbols),8)} symbols served by alpaca-bars-fallback (Alpha down)")
        log.warning("[VOL] %d symbols on alpaca-bars-fallback this run", fallback_used)

    # Only persist when at least one fresh fetch succeeded: writing a
    # default-only map would poison the cache (as happened on 2026-08-25).
    if fresh > 0:
        try:
            LOG_DIR.mkdir(exist_ok=True)
            CACHE_FILE.write_text(json.dumps({
                "_timestamp": time.time(),
                "volatility": list(vol_map.values())
            }, indent=2))
        except Exception as e:
            log.debug("[SWALLOWED] volatility cache write failed: %r", e)
            pass

    return vol_map

def adapt_delta_by_iv(symbol: str, vol_map: Dict[str, Dict], vix: float = 15.6, base_delta_max: float = 0.35) -> Dict:
    sym = symbol.upper()
    iv_rank = 50.0
    rv20 = 20.0
    if sym in vol_map:
        iv_rank = vol_map[sym].get("iv_rank_proxy", 50.0)
        rv20 = vol_map[sym].get("rv_20d", 20.0)

    # Sophie rule adapted:
    # VIX >25 or IVRank>50 => delta 0.20 conservative
    # VIX 15-25 medium => 0.30 balanced
    # VIX <15 low => wait but if must, 0.25 small
    if vix >= 35 or iv_rank >= 80:
        delta_max = 0.18
        regime = "extreme"
    elif vix >= 25 or iv_rank >= 50:
        delta_max = 0.20
        regime = "high"
    elif vix >= 15:
        delta_max = base_delta_max  # 0.35 medium
        regime = "medium"
    else:
        delta_max = 0.25
        regime = "low"

    # Adjust by RV
    if rv20 > 40:
        delta_max = min(delta_max, 0.20)
    elif rv20 < 12:
        delta_max = min(delta_max, 0.25)

    return {
        "delta_max": delta_max,
        "delta_min": 0.18,
        "iv_rank": iv_rank,
        "rv_20d": rv20,
        "vix": vix,
        "regime": regime,
        "reason": f"VIX {vix:.1f} {regime}, IVRank {iv_rank:.0f} proxy, RV20 {rv20:.1f}% -> delta_max {delta_max}"
    }

def get_volatility_report(symbols: List[str], vix: float = 15.6) -> Dict:
    v_map = build_cache(symbols)
    report = {}
    for sym in symbols:
        up = sym.upper()
        adapted = adapt_delta_by_iv(up, v_map, vix)
        report[up] = adapted
    return report
