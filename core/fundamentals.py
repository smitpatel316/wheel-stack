"""Fundamentals v2.5 - Alpha Vantage COMPANY_OVERVIEW + BALANCE_SHEET Debt/Eq + cache
Sophie filters: P/E<25, Debt/Eq<0.7, div>1.5%, mkt cap, beta
v2.5 adds true Debt/Equity via BALANCE_SHEET for robustness.
"""
import os
import json
import logging
import time
from pathlib import Path
from typing import Dict, List

from core.data_fallbacks import fetch_overview_finnhub

logger = logging.getLogger(f"strategy.{__name__}")

LOG_DIR = Path(__file__).parent.parent / "logs"
CACHE_FILE = LOG_DIR / "fundamentals_cache.json"
CACHE_TTL = 24*3600

def get_alpha_key():
    try:
        from config.credentials import ALPHA_VANTAGE_API_KEY
        return ALPHA_VANTAGE_API_KEY
    except Exception as e:
        logger.debug("[SWALLOWED] loading ALPHA_VANTAGE_API_KEY from config.credentials, falling back to env: %r", e)
        return os.getenv("ALPHA_VANTAGE_API_KEY") or ""

def _redact(err: Exception, key: str) -> str:
    """repr(err) with the API key scrubbed (requests exceptions embed the full URL)."""
    s = repr(err)
    return s.replace(key, "***") if key else s

def fetch_overview_alpha(symbol: str) -> Dict:
    key = get_alpha_key()
    if not key:
        return {}
    import requests
    url = "https://www.alphavantage.co/query"
    params = {"function": "OVERVIEW", "symbol": symbol, "apikey": key}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "Note" in data or "Information" in data:
            time.sleep(1.5)
            return {}
        return data
    except Exception as e:
        logger.warning("[SWALLOWED] Alpha OVERVIEW fetch failed for %s: %s", symbol, _redact(e, key))
        print(f"[FUND] Overview {symbol} failed: {_redact(e, key)}")
        return {}

def fetch_balance_sheet_alpha(symbol: str) -> Dict:
    key = get_alpha_key()
    if not key:
        return {}
    import requests
    url = "https://www.alphavantage.co/query"
    params = {"function": "BALANCE_SHEET", "symbol": symbol, "apikey": key}
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if "Note" in data or "Information" in data:
            time.sleep(2)
            return {}
        annual = data.get("annualReports", [])
        if not annual:
            return {}
        latest = annual[0]
        # Compute Debt/Equity
        try:
            equity = float(latest.get("totalShareholderEquity") or 0)
            short_debt = float(latest.get("shortTermDebt") or latest.get("currentDebt") or 0)
            long_debt = float(latest.get("longTermDebt") or 0)
            total_debt = short_debt + long_debt
            # Fallback: if no debt breakdown, use totalLiabilities / equity as proxy
            if total_debt == 0:
                total_debt = float(latest.get("totalLiabilities") or 0)
            debt_eq = total_debt / equity if equity > 0 else 0
            return {"DebtEquity": debt_eq, "totalDebt": total_debt, "equity": equity, "raw": latest}
        except Exception as e:
            logger.debug("[SWALLOWED] computing Debt/Equity from balance sheet for %s: %r", symbol, e)
            return {}
    except Exception as e:
        logger.warning("[SWALLOWED] Alpha BALANCE_SHEET fetch failed for %s: %s", symbol, _redact(e, key))
        # print(f"[FUND] Balance {symbol} failed: {e}")
        return {}

def build_cache(symbols: List[str]) -> Dict[str, Dict]:
    cache: Dict[str, Dict] = {}
    stale: Dict[str, Dict] = {}  # any-age on-disk entries, used as fallback on fetch failure
    if CACHE_FILE.exists():
        try:
            raw = json.loads(CACHE_FILE.read_text())
            ts = raw.get("_timestamp", 0)
            for entry in raw.get("fundamentals", []):
                sym = entry.get("symbol", "").upper()
                if sym in set(s.upper() for s in symbols):
                    stale[sym] = entry
            if time.time() - ts < CACHE_TTL:
                cache.update(stale)
                if cache:
                    print(f"[FUND] Cache hit {len(cache)}")
                    return cache
        except Exception as e:
            logger.debug("[SWALLOWED] loading fundamentals cache %s, rebuilding from APIs: %r", CACHE_FILE, e)
            pass

    overview_map: Dict[str, Dict] = {}
    fallback_used = 0
    # Phase 1: overview for all (Alpha -> Finnhub metric fallback -> stale cache)
    for sym in symbols[:12]:
        data = fetch_overview_alpha(sym)
        if data and data.get("Symbol"):
            overview_map[sym.upper()] = {
                "symbol": sym.upper(),
                "PERatio": data.get("PERatio"),
                "DividendYield": data.get("DividendYield"),
                "MarketCapitalization": data.get("MarketCapitalization"),
                "ProfitMargin": data.get("ProfitMargin"),
                "Volume": data.get("Volume") or data.get("AverageVolume"),
                "ROE": data.get("ReturnOnEquityTTM"),
                "Beta": data.get("Beta"),
                "Sector": data.get("Sector"),
                "AnalystTargetPrice": data.get("AnalystTargetPrice"),
                "ExDividendDate": data.get("ExDividendDate"),
                "QuarterlyEarningsGrowthYOY": data.get("QuarterlyEarningsGrowthYOY"),
                "QuarterlyRevenueGrowthYOY": data.get("QuarterlyRevenueGrowthYOY"),
                "DebtEquity": None,
                "source": "alpha_overview",
            }
        else:
            fb = fetch_overview_finnhub(sym)
            if fb:
                fb["symbol"] = sym.upper()
                overview_map[sym.upper()] = fb
                fallback_used += 1
                print(f"[FUND] {sym.upper()} via finnhub-fallback")
                logger.info("[FUND] %s fundamentals served by finnhub-fallback", sym.upper())
        time.sleep(0.6)

    if fallback_used:
        print(f"[FUND] {fallback_used}/{min(len(symbols),12)} symbols served by finnhub-fallback (Alpha down)")
        logger.warning("[FUND] %d symbols on finnhub-fallback this run", fallback_used)

    # Phase 2: balance sheet for debt/equity (only for high priority, rate limited 5/min).
    # Finnhub-fallback entries already carry DebtEquity (longTermDebt/equityAnnual) -
    # skip them so a flaky Alpha doesn't overwrite good fallback data with nothing.
    for sym in symbols[:8]:  # fewer to avoid rate limit
        if sym.upper() in overview_map:
            if overview_map[sym.upper()].get("source") == "finnhub_metric":
                continue
            bs = fetch_balance_sheet_alpha(sym)
            if bs and bs.get("DebtEquity") is not None:
                overview_map[sym.upper()]["DebtEquity"] = bs["DebtEquity"]
                overview_map[sym.upper()]["totalDebt"] = bs.get("totalDebt")
            time.sleep(1.0)  # balance sheet heavier, 1s gap

    fresh = len(overview_map)
    for k, v in stale.items():
        if k not in overview_map:
            overview_map[k] = v
    if fresh == 0 and stale:
        age_h = 0.0
        try:
            age_h = (time.time() - json.loads(CACHE_FILE.read_text()).get("_timestamp", 0)) / 3600
        except Exception as e:
            logger.debug("[SWALLOWED] computing fundamentals cache age for log line: %r", e)
        print(f"[FUND] All Alpha fetches failed - serving stale cache ({len(stale)} symbols, age {age_h:.1f}h); fundamentals screen degraded")
        logger.warning("[FUND] all Alpha overview fetches failed; using stale cache (%d symbols)", len(stale))
    elif fresh == 0 and not stale:
        print("[FUND] WARNING: NO fundamentals data (all Alpha fetches failed, cache empty) - fundamentals screen INACTIVE this run; all symbols pass unscreened")
        logger.warning("[FUND] no fundamentals data (fetch failed, cache empty); screen inactive this run")

    # Never overwrite the cache when every fetch failed: the old file keeps its
    # old timestamp so the next run retries the APIs instead of TTL-blocking
    # on stale data for another 24h.
    if fresh > 0:
        try:
            LOG_DIR.mkdir(exist_ok=True)
            CACHE_FILE.write_text(json.dumps({
                "_timestamp": time.time(),
                "fundamentals": list(overview_map.values())
            }, indent=2))
        except Exception as e:
            logger.debug("[SWALLOWED] writing fundamentals cache %s: %r", CACHE_FILE, e)
            pass

    return overview_map

def evaluate_fundamentals(symbol: str, fund_map: Dict[str, Dict], pe_max: float = 25.0, debt_eq_max: float = 0.7, mkt_cap_min: float = 1e9) -> Dict:
    sym = symbol.upper()
    if sym not in fund_map:
        return {"blocked": False, "reason": "No fundamentals", "score_modifier": 1.0}
    f = fund_map[sym]
    reasons = []
    blocked = False
    score_mod = 1.0

    try:
        pe = float(f.get("PERatio") or 0)
        if pe > 0 and pe > pe_max:
            if pe > pe_max*2:
                blocked = True
                reasons.append(f"P/E {pe:.1f} > {pe_max}x2 extreme")
            else:
                score_mod *= 0.9
                reasons.append(f"P/E {pe:.1f} > {pe_max} (high)")
    except Exception as e:
        logger.debug("[SWALLOWED] parsing PERatio for %s: %r", sym, e)
        pass

    try:
        de = f.get("DebtEquity")
        if de is not None:
            de_f = float(de)
            if de_f > debt_eq_max*2.5:  # >1.75 extreme
                blocked = True
                reasons.append(f"D/E {de_f:.2f} > {debt_eq_max*2.5:.1f} extreme leverage")
            elif de_f > debt_eq_max:
                score_mod *= 0.92
                reasons.append(f"D/E {de_f:.2f} > {debt_eq_max} (leveraged)")
    except Exception as e:
        logger.debug("[SWALLOWED] parsing DebtEquity for %s: %r", sym, e)
        pass

    try:
        mc = float(f.get("MarketCapitalization") or 0)
        if mc and mc < mkt_cap_min:
            score_mod *= 0.85
            reasons.append(f"Small cap ${mc/1e9:.1f}B < $1B")
    except Exception as e:
        logger.debug("[SWALLOWED] parsing MarketCapitalization for %s: %r", sym, e)
        pass

    try:
        dy = float(f.get("DividendYield") or 0) * 100
        if dy > 1.5:
            score_mod *= 1.05
    except Exception as e:
        logger.debug("[SWALLOWED] parsing DividendYield for %s: %r", sym, e)
        pass

    try:
        beta = float(f.get("Beta") or 1)
        if beta > 2.0:
            score_mod *= 0.9
            reasons.append(f"High Beta {beta:.1f}")
    except Exception as e:
        logger.debug("[SWALLOWED] parsing Beta for %s: %r", sym, e)
        pass

    # Growth screen v2.6: both revenue and earnings shrinking YoY -> block.
    # One shrinking -> small penalty. Missing data -> no opinion (never block).
    try:
        from config.params import GROWTH_BLOCK_ENABLED
    except Exception as e:
        logger.debug("[SWALLOWED] importing GROWTH_BLOCK_ENABLED from config.params, defaulting to True: %r", e)
        GROWTH_BLOCK_ENABLED = True
    try:
        eg_raw = f.get("QuarterlyEarningsGrowthYOY")
        rg_raw = f.get("QuarterlyRevenueGrowthYOY")
        eg = float(eg_raw) if eg_raw not in (None, "", "None") else None
        rg = float(rg_raw) if rg_raw not in (None, "", "None") else None
        if eg is not None and rg is not None:
            if eg < 0 and rg < 0:
                if GROWTH_BLOCK_ENABLED:
                    blocked = True
                    reasons.append(f"Growth block: revenue {rg:+.0%} and EPS {eg:+.0%} both shrinking YoY - deteriorating name")
                else:
                    score_mod *= 0.9
                    reasons.append(f"Growth: revenue {rg:+.0%} and EPS {eg:+.0%} shrinking YoY")
            elif eg < 0 or rg < 0:
                score_mod *= 0.95
                reasons.append(f"Growth mixed: revenue {rg:+.0%}, EPS {eg:+.0%}")
    except Exception as e:
        logger.debug("[SWALLOWED] parsing quarterly growth metrics for %s: %r", sym, e)
        pass

    reason_str = "; ".join(reasons) if reasons else "OK"
    return {"blocked": blocked, "reason": reason_str, "score_modifier": score_mod, "data": f}

def get_fundamentals_report(symbols: List[str]) -> Dict:
    f_map = build_cache(symbols)
    report = {}
    for sym in symbols:
        up = sym.upper()
        eval_r = evaluate_fundamentals(up, f_map)
        report[up] = eval_r
    return report
