"""Backup market-data sources, used only when Alpha Vantage fails.

Chain per dataset (fail-stale on-disk cache stays the last resort):
  fundamentals : Alpha OVERVIEW/BALANCE_SHEET -> Finnhub /stock/metric -> stale cache
  vol closes   : Alpha TIME_SERIES_DAILY      -> Alpaca daily bars     -> stale cache
  volume trend : Alpha TIME_SERIES_DAILY      -> Alpaca daily bars     -> stale cache
  dividends    : Alpha -> Finnhub /stock/dividend (already in core/dividend_calendar;
                 Finnhub free tier 403s on that endpoint as of 2026-08-26, verified)

Auto-enable: each fetcher returns nothing unless its keys are present in the
environment (or .env). Placeholder values like '***REMOVED***...' count as
missing - this repo is public, keys are bring-your-own. No real key is ever a
default here.

Field-mapping choices (Finnhub /stock/metric?metric=all -> Alpha OVERVIEW schema):
  peTTM                              -> PERatio
  dividendYieldIndicatedAnnual / 100 -> DividendYield   (Finnhub sends percent)
  marketCapitalization * 1e6         -> MarketCapitalization (Finnhub sends $M)
  beta                               -> Beta
  longTermDebt/equityAnnual          -> DebtEquity  (NOT totalDebt/totalEquityAnnual:
      Finnhub's total-debt variant runs ~2x Alpha's short+long-debt/equity for
      banks - 2.33 vs ~0.9 for BAC - and would false-block them. longTermDebt/
      equityAnnual is the closest methodological match to the Alpha computation.)
  epsGrowthQuarterlyYoy / 100        -> QuarterlyEarningsGrowthYOY (percent->fraction)
  revenueGrowthQuarterlyYoy / 100    -> QuarterlyRevenueGrowthYOY
  3MonthAverageTradingVolume * 1e6   -> Volume (Finnhub sends millions of shares)
  ExDividendDate                     -> unavailable on Finnhub free tier (left None)

Alpaca daily bars: data.alpaca.markets /v2/stocks/{symbol}/bars, timeframe=1Day,
feed=iex (free plan), adjustment=raw, explicit start date (without `start` the
API returns only the current session's bar). IEX-only volume is directionally
fine for trend scoring.
"""

import logging
import os
import time
from datetime import date, timedelta
from typing import Dict, List

logger = logging.getLogger(f"strategy.{__name__}")

_FINNHUB_METRIC_URL = "https://finnhub.io/api/v1/stock/metric"
_ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"


def _env_key(name: str) -> str:
    """Key from the process env only; placeholders and empty strings count as
    missing. (.env loading is config.credentials' job - it runs at engine
    startup before any of these fetchers are called.)"""
    val = os.getenv(name) or ""
    if not val or val.startswith("***") or "REMOVED" in val:
        return ""
    return val


def finnhub_enabled() -> bool:
    return bool(_env_key("FINNHUB_API_KEY"))


def alpaca_data_enabled() -> bool:
    return bool(_env_key("ALPACA_API_KEY") and _env_key("ALPACA_SECRET_KEY"))


def _redact(err: Exception, *secrets: str) -> str:
    s = repr(err)
    for sec in secrets:
        if sec:
            s = s.replace(sec, "***")
    return s


def fetch_overview_finnhub(symbol: str) -> Dict:
    """One Finnhub /stock/metric?metric=all call -> Alpha OVERVIEW-shaped dict.

    Returns {} when Finnhub is not configured or the fetch fails.
    """
    key = _env_key("FINNHUB_API_KEY")
    if not key:
        return {}
    import requests
    try:
        r = requests.get(_FINNHUB_METRIC_URL,
                         params={"symbol": symbol, "metric": "all", "token": key},
                         timeout=15)
        if r.status_code == 429:
            time.sleep(1)
            r = requests.get(_FINNHUB_METRIC_URL,
                             params={"symbol": symbol, "metric": "all", "token": key},
                             timeout=15)
        if r.status_code == 403:
            logger.warning("[FUND] Finnhub metric 403 for %s (plan restriction?) - fundamentals fallback unavailable this run", symbol)
            return {}
        r.raise_for_status()
        m = (r.json() or {}).get("metric") or {}
        if not m:
            return {}

        def _f(name):
            v = m.get(name)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError) as e:
                logger.debug("[SWALLOWED] Finnhub metric field %s for %s not numeric (%r): %r", name, symbol, v, e)
                return None

        pe = _f("peTTM")
        dy = _f("dividendYieldIndicatedAnnual")
        mc = _f("marketCapitalization")
        de = _f("longTermDebt/equityAnnual")
        eg = _f("epsGrowthQuarterlyYoy")
        rg = _f("revenueGrowthQuarterlyYoy")
        beta = _f("beta")
        avg_vol = _f("3MonthAverageTradingVolume")
        roe = _f("roeTTM")
        pm = _f("netProfitMarginTTM")
        target = _f("targetMeanPrice")
        return {
            "symbol": symbol.upper(),
            "PERatio": pe,
            "DividendYield": (dy / 100.0) if dy is not None else None,
            "MarketCapitalization": (mc * 1e6) if mc is not None else None,
            "ProfitMargin": (pm / 100.0) if pm is not None else None,
            "Volume": (avg_vol * 1e6) if avg_vol is not None else None,
            "ROE": (roe / 100.0) if roe is not None else None,
            "Beta": beta,
            "Sector": None,  # not in metric payload; not worth a second call
            "AnalystTargetPrice": target,
            "ExDividendDate": None,  # Finnhub free tier has no ex-div access (403 verified 2026-08-26)
            "QuarterlyEarningsGrowthYOY": (eg / 100.0) if eg is not None else None,
            "QuarterlyRevenueGrowthYOY": (rg / 100.0) if rg is not None else None,
            "DebtEquity": de,
            "totalDebt": None,  # ratio only; absolute debt not needed by the screen
            "source": "finnhub_metric",
        }
    except Exception as e:
        logger.warning("[SWALLOWED] Finnhub metric fetch failed for %s: %s", symbol, _redact(e, key))
        print(f"[FUND] Finnhub metric {symbol} failed: {_redact(e, key)}")
        return {}


def fetch_daily_bars_alpaca(symbol: str, days: int = 300) -> List[Dict]:
    """Alpaca IEX daily bars -> [{date, close, volume}...] oldest-first.

    Returns [] when Alpaca keys are not configured or the fetch fails.
    `days` counts calendar days back for the start bound (bars returned are
    trading days only, so the list is shorter - 300 calendar days ~= 205 bars).
    """
    ak = _env_key("ALPACA_API_KEY")
    sk = _env_key("ALPACA_SECRET_KEY")
    if not (ak and sk):
        return []
    import requests
    start = (date.today() - timedelta(days=days)).isoformat()
    try:
        r = requests.get(
            _ALPACA_BARS_URL.format(symbol=symbol.upper()),
            params={"timeframe": "1Day", "start": start, "limit": 1000,
                    "feed": "iex", "adjustment": "raw"},
            headers={"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": sk},
            timeout=20,
        )
        r.raise_for_status()
        bars = (r.json() or {}).get("bars") or []
        out = []
        for b in bars:
            try:
                out.append({"date": str(b["t"])[:10], "close": float(b["c"]),
                            "volume": int(b.get("v") or 0)})
            except Exception as e:
                logger.debug("[SWALLOWED] parsing Alpaca bar for %s: %r", symbol, e)
                continue
        return out
    except Exception as e:
        logger.warning("[SWALLOWED] Alpaca daily bars fetch failed for %s: %s", symbol, _redact(e, ak, sk))
        print(f"[FALLBACK] Alpaca bars {symbol} failed: {_redact(e, ak, sk)}")
        return []
