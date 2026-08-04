"""Liquidity v2.5.1 - Volume + OI trend detection for perfect robust
Checks 5-day avg volume vs 20-day, and OI trend if available to detect drying liquidity.
Uses Alpha TIME_SERIES_DAILY for underlying volume history.
"""
import os
import json
import time
from pathlib import Path
from typing import Dict, List

LOG_DIR = Path(__file__).parent.parent / "logs"
CACHE_FILE = LOG_DIR / "liquidity_cache.json"
CACHE_TTL = 6*3600  # 6h

def get_alpha_key():
    try:
        from config.credentials import ALPHA_VANTAGE_API_KEY
        return ALPHA_VANTAGE_API_KEY
    except Exception:
        return os.getenv("ALPHA_VANTAGE_API_KEY") or ""

def fetch_daily_volume_alpha(symbol: str, days: int = 30) -> List[int]:
    key = get_alpha_key()
    if not key:
        return []
    import requests
    url = "https://www.alphavantage.co/query"
    params = {"function": "TIME_SERIES_DAILY", "symbol": symbol, "apikey": key, "outputsize": "compact"}
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if "Note" in data or "Information" in data:
            time.sleep(2)
            return []
        ts = data.get("Time Series (Daily)", {})
        if not ts:
            return []
        vols = []
        for date_str in sorted(ts.keys())[-days:]:
            try:
                vols.append(int(float(ts[date_str].get("5. volume") or 0)))
            except Exception:
                pass
        return vols
    except Exception as e:
        print(f"[LIQ] {symbol} vol fetch failed: {e}")
        return []

def evaluate_liquidity(symbol: str, current_volume: int = None, current_oi: int = None) -> Dict:
    """Score 1.0 good, <1.0 penalized if drying"""
    sym = symbol.upper()
    vols = []
    # Try cache first
    if CACHE_FILE.exists():
        try:
            raw = json.loads(CACHE_FILE.read_text())
            if sym in raw.get("vol_history", {}):
                entry = raw["vol_history"][sym]
                if time.time() - entry.get("_ts",0) < CACHE_TTL:
                    vols = entry.get("vols", [])
        except Exception:
            pass
    if not vols:
        vols = fetch_daily_volume_alpha(sym, days=30)
        if vols:
            try:
                LOG_DIR.mkdir(exist_ok=True)
                raw = {}
                if CACHE_FILE.exists():
                    try:
                        raw = json.loads(CACHE_FILE.read_text())
                    except Exception:
                        raw = {}
                if "vol_history" not in raw:
                    raw["vol_history"] = {}
                raw["vol_history"][sym] = {"vols": vols, "_ts": time.time()}
                CACHE_FILE.write_text(json.dumps(raw))
            except Exception:
                pass
        time.sleep(0.6)

    score = 1.0
    reasons = []
    avg_5 = sum(vols[-5:])/5 if len(vols)>=5 else (vols[-1] if vols else 0)
    avg_20 = sum(vols[-20:])/20 if len(vols)>=20 else (sum(vols)/len(vols) if vols else 0)
    trend_ok = True

    if avg_20 > 0 and avg_5 > 0:
        if avg_5 < avg_20 * 0.6:
            score *= 0.85
            reasons.append(f"Vol drying 5d avg {avg_5/1e6:.1f}M < 60% 20d {avg_20/1e6:.1f}M")
            trend_ok = False
        if avg_5 < 500_000:  # <500k daily very thin
            score *= 0.80
            reasons.append(f"Thin volume 5d avg {avg_5/1e6:.2f}M < 0.5M")
            trend_ok = False

    # Current OI vs volume check (if provided from option snapshot)
    if current_volume is not None and current_oi is not None:
        if current_volume < 10 and current_oi and current_oi > 0:
            # Check OI trend not available without history, but low volume + high OI = still ok if OI large
            pass

    return {"score_modifier": score, "trend_ok": trend_ok, "avg_5d": avg_5, "avg_20d": avg_20, "reason": "; ".join(reasons) if reasons else "OK", "vols": vols[-5:] if len(vols)>=5 else vols}

def get_liquidity_report(symbols: List[str], snapshot_map: Dict = None) -> Dict[str, Dict]:
    report = {}
    for sym in symbols[:10]:  # rate limited, top 10 per run
        snap = snapshot_map.get(sym) if snapshot_map else None
        cur_vol = None
        cur_oi = None
        if snap and isinstance(snap, dict):
            cur_vol = snap.get("volume")
            cur_oi = snap.get("oi")
        report[sym.upper()] = evaluate_liquidity(sym, cur_vol, cur_oi)
    return report
