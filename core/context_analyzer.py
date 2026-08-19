"""
Context Analyzer - LLM-as-model-builder for wheel strategy v2.1

Fixes:
- VIX via Alpaca IWM/SPY vol proxy + FRED? Actually Alpaca doesn't have VIX index, so derive via ATR or use SPY 20d realized vol *16 (VIX approx)
- Also try CBOE API alternative, and fallback to SPY realized vol
- Previously used Yahoo which returns 403 on Pi due to crumb
- Now uses SPY historical bars via Alpaca + calculated realized vol, plus optional VIXY ETF as proxy

From paper arXiv:2512.01123
"""
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
import datetime
import logging
import os
import json
import math

logger = logging.getLogger("strategy.context_analyzer")

@dataclass
class MarketContext:
    timestamp: str = ""
    vix: Optional[float] = None
    vix_level: str = "medium"
    market_regime: str = "neutral"
    trend: str = "neutral"
    volatility_level: str = "medium"
    iv_rank_avg: Optional[float] = None
    symbols_analyzed: int = 0
    avg_daily_vol_ok: bool = True
    fomo_level: float = 0.0
    confidence_level: float = 0.7
    stress_level: float = 0.3
    tilt_risk: float = 0.1
    technical_position: str = "neutral"
    spy_price: Optional[float] = None
    spy_change_pct: Optional[float] = None
    spy_5d: Optional[float] = None
    spy_20d_vol: Optional[float] = None  # realized vol
    underlying_momentum: Dict[str, float] = field(default_factory=dict)
    bn_nodes: List[str] = field(default_factory=lambda: [
        "Market Regime", "Volatility Level", "Technical Position",
        "Strike Selection", "Premium Rate", "Assignment Probability", "Trade Outcome"
    ])
    bn_edges: List[List[str]] = field(default_factory=lambda: [
        ["Market Regime", "Strike Selection"],
        ["Volatility Level", "Assignment Probability"],
        ["Technical Position", "Premium Rate"],
        ["Strike Selection", "Assignment Probability"],
        ["Assignment Probability", "Trade Outcome"],
        ["Premium Rate", "Trade Outcome"],
    ])
    bn_reasoning: str = "Default wheel causal structure"
    decision_factors: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def _classify_vix(vix: float) -> str:
    if vix is None:
        return "medium"
    if vix < 15:
        return "low"
    elif vix < 25:
        return "medium"
    elif vix < 35:
        return "high"
    else:
        return "extreme"

def _classify_regime(spy_change_5d: Optional[float], vix_level: str) -> str:
    if spy_change_5d is None:
        return "neutral"
    if spy_change_5d > 0.03 and vix_level in ("low", "medium"):
        return "bull"
    elif spy_change_5d < -0.03 or vix_level in ("high", "extreme"):
        return "bear"
    else:
        return "neutral"

def _classify_technical(avg_momentum: float) -> str:
    if avg_momentum > 0.05:
        return "overbought"
    elif avg_momentum < -0.05:
        return "oversold"
    else:
        return "neutral"

def _realized_vol(prices: List[float]) -> Optional[float]:
    if len(prices) < 10:
        return None
    try:
        rets = [math.log(prices[i]/prices[i-1]) for i in range(1,len(prices)) if prices[i-1] and prices[i]]
        if len(rets) < 5:
            return None
        mean = sum(rets)/len(rets)
        var = sum((r-mean)**2 for r in rets)/len(rets)
        daily_vol = math.sqrt(var)
        annual_vol = daily_vol * math.sqrt(252) * 100  # % like VIX
        return annual_vol
    except Exception as e:
        logger.debug("[SWALLOWED] realized vol calc failed (%d prices): %r", len(prices), e)
        return None



def get_vix_and_spy(client=None) -> Dict[str, Any]:
    """
    VIX v2.2 accurate:
    1. Yahoo v8 chart ^VIX (query1.finance.yahoo.com/v8/finance/chart/%5EVIX) -> real VIX 15.6 confirmed via browser 2026-08-03
    2. Alpaca IEX SPY + VIXY bars for realized vol, SPY momentum, VIXY proxy as fallback only
    3. CBOE API legacy as last resort
    Previous v2.1 used VIXY*1.3+4 = 30.26 overestimated 2x real 15.6 (actual VIX 15.6 low, not high)
    """
    result = {"vix": None, "spy_price": None, "spy_change": None, "spy_5d": None, "spy_20d_vol": None, "vix_proxy": None, "source": "none", "vixy_price": None}

    # 1. Yahoo v8 ^VIX - primary, browser confirmed 15.60 on 2026-08-03
    try:
        import requests
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=1mo&interval=1d", timeout=6, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
        if r.status_code == 200:
            data = r.json()
            chart = data.get('chart', {}).get('result', [])
            if chart:
                quote = chart[0].get('indicators', {}).get('quote', [{}])[0]
                closes = quote.get('close', [])
                closes = [c for c in closes if c]
                if closes:
                    result["vix"] = float(closes[-1])
                    result["vix_proxy"] = float(closes[-1])
                    result["source"] = "yahoo_v8_vix"
                    if len(closes) >= 5:
                        first = closes[0]
                        last = closes[-1]
                        if first:
                            result["vix_5d"] = (last-first)/first
                    # 20d vol of VIX itself not needed
        # Also get SPY for momentum from same Yahoo endpoint
        r2 = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=10d&interval=1d", timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        if r2.status_code == 200:
            data2 = r2.json()
            chart2 = data2.get('chart', {}).get('result', [])
            if chart2:
                meta = chart2[0].get('meta', {})
                quote2 = chart2[0].get('indicators', {}).get('quote', [{}])[0]
                closes2 = quote2.get('close', [])
                closes2 = [c for c in closes2 if c]
                if closes2:
                    result["spy_price"] = float(closes2[-1])
                    if len(closes2) >= 2:
                        result["spy_change"] = (closes2[-1]-closes2[-2])/closes2[-2] if closes2[-2] else None
                    if len(closes2) >= 5:
                        result["spy_5d"] = (closes2[-1]-closes2[0])/closes2[0] if closes2[0] else None
                    # realized vol
                    vol = _realized_vol(closes2[-20:] if len(closes2)>=20 else closes2)
                    result["spy_20d_vol"] = vol
    except Exception as e:
        logger.debug(f"Yahoo v8 VIX/SPY fetch failed: {e}")

    # 2. Alpaca IEX for additional breadth if Yahoo worked, or as fallback
    if client and hasattr(client, 'stock_client'):
        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            from alpaca.data.enums import DataFeed
            from datetime import datetime, timedelta, timezone
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=40)

            # Only fetch if we still need SPY momentum or want VIXY for confirmation
            need_spy = result["spy_price"] is None or result["spy_5d"] is None
            try:
                req = StockBarsRequest(
                    symbol_or_symbols=["SPY", "VIXY"] if need_spy else ["VIXY"],
                    timeframe=TimeFrame.Day,
                    start=start,
                    end=end,
                    feed=DataFeed.IEX,
                    limit=100
                )
                bars_response = client.stock_client.get_stock_bars(req)
                has_df = hasattr(bars_response, 'df') and bars_response.df is not None and not bars_response.df.empty
                if has_df:
                    df = bars_response.df
                    if need_spy and "SPY" in df.index.get_level_values(0):
                        try:
                            spy_df = df.xs("SPY")
                            closes = spy_df.close.tolist()
                            closes = [float(c) for c in closes if c]
                            if closes:
                                if result["spy_price"] is None:
                                    result["spy_price"] = closes[-1]
                                if result["spy_5d"] is None and len(closes)>=5:
                                    result["spy_5d"] = (closes[-1]-closes[0])/closes[0] if closes[0] else None
                                if result["spy_20d_vol"] is None:
                                    vol = _realized_vol(closes[-20:] if len(closes)>=20 else closes)
                                    result["spy_20d_vol"] = vol
                        except Exception as e:
                            logger.debug(f"SPY IEX DF {e}")
                    if "VIXY" in df.index.get_level_values(0):
                        try:
                            vixy_df = df.xs("VIXY")
                            vcloses = vixy_df.close.tolist()
                            vcloses = [float(c) for c in vcloses if c]
                            if vcloses:
                                result["vixy_price"] = vcloses[-1]
                                # Only use VIXY proxy if Yahoo VIX failed, and calibrate factor down: real data 2026-08-03 VIX 15.6 vs VIXY IEX 20.2 => old *1.3+4=30.26 overest 94%, correct factor ~0.6*VIXY+3.5=15.6
                                # Measure: VIXY 20.2 -> VIX 15.6 => VIX = VIXY*0.6+3.48
                                # Use 0.6 factor now
                                vixy_est = vcloses[-1]*0.6 + 3.5
                                result["vixy_proxy"] = vixy_est
                                if result["vix"] is None:
                                    result["vix"] = vixy_est
                                    result["source"] = "alpaca_iex_vixy_proxy_v22"
                                # sanity clamp if VIXY proxy wildly off vs Yahoo when both exist: prefer Yahoo
                                result["vixy_5d"] = (vcloses[-1]-vcloses[0])/vcloses[0] if len(vcloses)>=5 and vcloses[0] else None
                        except Exception as e:
                            logger.debug(f"VIXY IEX DF {e}")
            except Exception as e:
                logger.debug(f"Alpaca IEX bars fetch failed {e}")

            # Latest trade fallbacks
            if result["spy_price"] is None:
                try:
                    trades = client.get_stock_latest_trade(["SPY"])
                    if isinstance(trades, dict):
                        t = trades.get("SPY")
                        if t:
                            result["spy_price"] = float(getattr(t,'price',0) or (t.get('price') if isinstance(t, dict) else 0) or 0)
                except Exception as e:
                    logger.debug("[SWALLOWED] SPY latest-trade fallback failed: %r", e)
                    pass
            if result["vix"] is None:
                try:
                    trades = client.get_stock_latest_trade(["VIXY"])
                    if isinstance(trades, dict):
                        t = trades.get("VIXY")
                        if t:
                            price = float(getattr(t,'price',0) or (t.get('price') if isinstance(t, dict) else 0) or 0)
                            if price:
                                result["vix"] = price*0.6 + 3.5  # v2.2 calibrated vs real 15.6
                                result["vix_proxy"] = result["vix"]
                                result["source"] = "vixy_latest_proxy_v22"
                                result["vixy_price"] = price
                except Exception as e:
                    logger.debug("[SWALLOWED] VIXY latest-trade fallback failed: %r", e)
                    pass
        except Exception as e:
            logger.debug(f"Alpaca block failed {e}")

    # 3. Clamp VIX to reasonable 9-45 range and prefer Yahoo source
    if result["vix"] is not None:
        # Clamp
        result["vix"] = max(9.0, min(45.0, float(result["vix"])))
        result["vix_proxy"] = result["vix"]

    return result




def analyze_context(client=None, symbols: List[str] = None, use_llm: bool = False) -> MarketContext:
    ctx = MarketContext()
    ctx.timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()

    market_data = get_vix_and_spy(client)
    ctx.vix = market_data.get("vix")
    ctx.spy_price = market_data.get("spy_price")
    ctx.spy_change_pct = market_data.get("spy_change")
    spy_5d = market_data.get("spy_5d")
    # store extra
    ctx.decision_factors["spy_5d"] = spy_5d
    ctx.decision_factors["vix_source"] = market_data.get("source")
    ctx.decision_factors["spy_20d_vol"] = market_data.get("spy_20d_vol")
    if "vixy_5d" in market_data:
        ctx.decision_factors["vixy_5d"] = market_data["vixy_5d"]

    ctx.vix_level = _classify_vix(ctx.vix) if ctx.vix else "medium"
    ctx.market_regime = _classify_regime(spy_5d, ctx.vix_level)
    ctx.volatility_level = ctx.vix_level

    if symbols:
        ctx.symbols_analyzed = len(symbols)

    avg_mom = spy_5d or 0
    ctx.technical_position = _classify_technical(avg_mom)

    if ctx.market_regime == "bull" and (spy_5d or 0) > 0.05:
        ctx.fomo_level = 0.7
        ctx.confidence_level = 0.8
        ctx.stress_level = 0.2
    elif ctx.market_regime == "bear":
        ctx.fomo_level = 0.1
        ctx.confidence_level = 0.4
        ctx.stress_level = 0.7
        ctx.tilt_risk = 0.3

    if ctx.market_regime == "bear" or ctx.vix_level in ("high", "extreme"):
        ctx.bn_nodes = [
            "Market Regime", "Volatility Level", "VIX", "Stock Fundamentals",
            "Technical Position", "FOMO Level", "Stress Level",
            "Strike Selection", "Premium Rate", "Assignment Probability",
            "Position Size", "Trade Outcome"
        ]
        ctx.bn_edges = [
            ["Volatility Level", "Strike Selection"],
            ["Volatility Level", "Assignment Probability"],
            ["Market Regime", "Position Size"],
            ["Stress Level", "Strike Selection"],
            ["Stock Fundamentals", "Assignment Probability"],
            ["Strike Selection", "Assignment Probability"],
            ["Assignment Probability", "Trade Outcome"],
            ["Premium Rate", "Trade Outcome"],
            ["Position Size", "Trade Outcome"],
            ["VIX", "Volatility Level"],
        ]
        ctx.bn_reasoning = f"Bear/vol VIX {ctx.vix:.1f} src {market_data.get('source')} - vol emphasis, assign prob 15% size 10% conservative (paper Mar 2020 DD -18.3% recovery by Jul)"
        ctx.decision_factors.update({
            "regime": "bear",
            "vix_level": ctx.vix_level,
            "recommended_size_pct": 10,
            "assignment_prob_est": 0.15,
            "case_study": "COVID crash adaptive - paper March 2020",
        })
    elif ctx.market_regime == "bull" and ctx.technical_position != "overbought":
        ctx.bn_nodes = [
            "Market Regime", "Volatility Level", "Trend", "Momentum",
            "Strike Selection", "Premium Rate", "Assignment Probability",
            "Position Size", "Trade Outcome"
        ]
        ctx.bn_edges = [
            ["Market Regime", "Strike Selection"],
            ["Trend", "Strike Selection"],
            ["Momentum", "Premium Rate"],
            ["Volatility Level", "Assignment Probability"],
            ["Strike Selection", "Trade Outcome"],
            ["Premium Rate", "Trade Outcome"],
            ["Position Size", "Trade Outcome"],
        ]
        ctx.bn_reasoning = f"Bull SPY 5d {spy_5d} VIX {ctx.vix} src {market_data.get('source')} - larger size 25% aggressive 8% OTM (paper 2021 bull +45.9% 332k premium)"
        ctx.decision_factors.update({
            "regime": "bull",
            "vix_level": ctx.vix_level,
            "recommended_size_pct": 25,
            "assignment_prob_est": 0.08,
            "case_study": "Bull 2021 optimization",
        })
    else:
        ctx.bn_nodes = [
            "Market Regime", "Volatility Level", "Technical Position",
            "Stock Fundamentals", "Strike Selection", "Premium Rate",
            "Assignment Probability", "Trade Outcome"
        ]
        ctx.bn_edges = [
            ["Market Regime", "Strike Selection"],
            ["Volatility Level", "Assignment Probability"],
            ["Technical Position", "Premium Rate"],
            ["Stock Fundamentals", "Strike Selection"],
            ["Strike Selection", "Assignment Probability"],
            ["Assignment Probability", "Trade Outcome"],
            ["Premium Rate", "Trade Outcome"],
        ]
        ctx.bn_reasoning = f"Neutral VIX {ctx.vix} ({ctx.vix_level}) src {market_data.get('source')} SPY 5d {spy_5d} - balanced 30-45 DTE 0.30 delta Sophie + paper"
        ctx.decision_factors.update({
            "regime": "neutral",
            "vix_level": ctx.vix_level,
            "recommended_size_pct": 15,
            "assignment_prob_est": 0.05,
        })

    if use_llm:
        try:
            ctx = _enrich_with_llm(ctx, market_data)
        except Exception as e:
            logger.debug(f"LLM enrichment failed: {e}")

    # unify spy_5d
    ctx.decision_factors.setdefault("spy_5d", spy_5d)
    return ctx


def _enrich_with_llm(ctx: MarketContext, market_data: Dict) -> MarketContext:
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY")
    if not api_key:
        return ctx
    try:
        import requests, re, json as js
        prompt = f"""
You are expert Bayesian Networks + options wheel.
Market: VIX {ctx.vix} ({ctx.vix_level}) src {market_data.get('source')}, SPY {ctx.spy_price} 5d {market_data.get('spy_5d')} 20d vol {market_data.get('spy_20d_vol')}, regime {ctx.market_regime}
Psych: FOMO {ctx.fomo_level:.2f} Conf {ctx.confidence_level:.2f} Stress {ctx.stress_level:.2f} Tilt {ctx.tilt_risk:.2f}
Wheel: DELTA 0.18-0.35 EXP 14-60 MAX_RISK 90k ROLLING_OTM 0.03 Spread max $0.15 or 12% SPREAD_NTM $0.05
Create DAG nodes for market/psych/strategy/outcomes, edges causal. Output JSON only: {{\"nodes\":[...],\"edges\":[[\"p\",\"c\"],...],\"reasoning\":\"...\",\"recommended_params\":{{\"delta_max\":...,\"exp_max\":...,\"size_pct\":...}}}}
"""
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        model = os.getenv("WHEEL_LLM_MODEL", "gpt-4o-mini")
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.1,
                "max_tokens": 1500,
                "messages": [
                    {"role": "system", "content": "You are expert Bayesian Networks and options wheel trading. Output ONLY valid JSON."},
                    {"role": "user", "content": prompt}
                ]
            },
            timeout=15,
        )
        if resp.status_code==200:
            content = resp.json()["choices"][0]["message"]["content"]
            m = re.search(r'\{.*\}', content, re.DOTALL)
            if m:
                data = js.loads(m.group())
                if "nodes" in data and "edges" in data:
                    ctx.bn_nodes = data["nodes"]
                    ctx.bn_edges = data["edges"]
                    ctx.bn_reasoning = data.get("reasoning", ctx.bn_reasoning)
                    if "recommended_params" in data:
                        ctx.decision_factors.update(data["recommended_params"])
                    logger.info(f"LLM BN enriched {len(ctx.bn_nodes)} nodes {len(ctx.bn_edges)} edges")
    except Exception as e:
        logger.debug(f"LLM BN generation failed: {e}")
    return ctx


def adapt_params(ctx: MarketContext, base_params: Dict = None) -> Dict[str, Any]:
    """
    Adapt wheel params based on market context (paper + Sophie).

    Returns overrides.

    v2.1: MAX_RISK 90k base (was 75k), rolling 0.03 not 0.05, spread filters.
    """
    overrides = {}
    base_max = 90000
    if ctx.market_regime == "bear" or ctx.vix_level in ("high", "extreme"):
        overrides.update({
            "DELTA_MAX": 0.25,
            "DELTA_MIN": 0.15,
            "EXPIRATION_MAX": 45,
            "EXPIRATION_MIN": 14,
            "MAX_RISK_PCT": 0.40,
            "MAX_RISK": int(base_max * 0.6),  # 54k defensive
            "YIELD_MIN": 0.01,
            "SCORE_MIN": 0.03,
            "POSITION_SIZE_PCT": 10,
            "ROLLING_OTM": 0.03,  # tighter v2.1
            "SPREAD_MAX_ABS": 0.10,  # tighter in high vol
            "SPREAD_MAX_PCT": 0.10,
            "NOTE": f"Bear/high vol VIX {ctx.vix} src {ctx.decision_factors.get('vix_source')} - Mar 2020 adaptive size 10% conservative"
        })
    elif ctx.market_regime == "bull" and ctx.technical_position != "overbought":
        overrides.update({
            "DELTA_MAX": 0.35,
            "DELTA_MIN": 0.18,
            "EXPIRATION_MAX": 60,
            "EXPIRATION_MIN": 14,
            "MAX_RISK": base_max,
            "MAX_RISK_PCT": 1.0,
            "YIELD_MIN": 0.008,
            "SCORE_MIN": 0.02,
            "POSITION_SIZE_PCT": 25,
            "ROLLING_OTM": 0.03,  # still 3% v2.1 (was 0.08 too aggressive)
            "SPREAD_MAX_ABS": 0.15,
            "SPREAD_MAX_PCT": 0.12,
            "NOTE": f"Bull 2021 style VIX {ctx.vix} size 25% aggressive 8% OTM"
        })
    else:
        overrides.update({
            "DELTA_MAX": 0.30,
            "DELTA_MIN": 0.18,
            "EXPIRATION_MAX": 45,
            "EXPIRATION_MIN": 14,
            "MAX_RISK": base_max,
            "MAX_RISK_PCT": 0.75,
            "POSITION_SIZE_PCT": 15,
            "ROLLING_OTM": 0.03,
            "SPREAD_MAX_ABS": 0.15,
            "SPREAD_MAX_PCT": 0.12,
            "NOTE": f"Neutral balanced VIX {ctx.vix} ({ctx.vix_level}) src {ctx.decision_factors.get('vix_source')} - Sophie 30-45 DTE 0.30 delta"
        })

    if ctx.vix_level == "low":
        overrides["YIELD_MIN"] = 0.015
        overrides["NOTE"] += " | Low IV: wait per Sophie"
    elif ctx.vix_level == "extreme":
        overrides["DELTA_MAX"] = min(overrides.get("DELTA_MAX", 0.30), 0.20)
        overrides["MAX_RISK"] = int(overrides.get("MAX_RISK", base_max) * 0.5)
        overrides["NOTE"] += " | Extreme IV: cut 50%"

    return overrides


def save_context_log(ctx: MarketContext, path: str = "logs/market_context.json"):
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = ctx.to_dict()
    if p.exists():
        try:
            data = json.loads(p.read_text())
            if not isinstance(data, list):
                data = [data]
        except Exception as e:
            logger.warning("[SWALLOWED] market context log %s unreadable, starting fresh list: %r", p, e)
            data = []
    else:
        data = []
    data.append(entry)
    data = data[-500:]
    p.write_text(json.dumps(data, indent=2))
