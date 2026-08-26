"""Unit tests for core/data_fallbacks.py and its wiring into
fundamentals / volatility / liquidity. All HTTP is mocked - no real
network, no live Optionable, no production cache files.
"""

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

import core.data_fallbacks as fb


# --------------------------------------------------------------------------
# env-key gating
# --------------------------------------------------------------------------

class TestKeyGating:
    def test_missing_keys_disable(self, monkeypatch):
        for n in ("FINNHUB_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
            monkeypatch.delenv(n, raising=False)
        assert not fb.finnhub_enabled()
        assert not fb.alpaca_data_enabled()
        assert fb.fetch_overview_finnhub("BAC") == {}
        assert fb.fetch_daily_bars_alpaca("BAC") == []

    def test_placeholder_keys_disable(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "***REMOVED***abc")
        monkeypatch.setenv("ALPACA_API_KEY", "***REMOVED***")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "***REMOVED***")
        assert not fb.finnhub_enabled()
        assert not fb.alpaca_data_enabled()

    def test_real_keys_enable(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        monkeypatch.setenv("ALPACA_API_KEY", "ak-test")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "sk-test")
        assert fb.finnhub_enabled()
        assert fb.alpaca_data_enabled()


# --------------------------------------------------------------------------
# Finnhub metric fetcher
# --------------------------------------------------------------------------

def _finnhub_payload():
    return {"metric": {
        "peTTM": 13.29,
        "dividendYieldIndicatedAnnual": 3.229,
        "marketCapitalization": 447361.06,   # $M
        "beta": 1.222,
        "longTermDebt/equityAnnual": 0.9103,
        "totalDebt/totalEquityAnnual": 2.3296,  # deliberately NOT mapped (banks)
        "epsGrowthQuarterlyYoy": 33.76,
        "revenueGrowthQuarterlyYoy": 71.37,
        "3MonthAverageTradingVolume": 33.79,   # millions of shares
        "roeTTM": 9.5,
        "netProfitMarginTTM": 26.1,
        "targetMeanPrice": 55.0,
    }}


class TestFinnhubOverview:
    def test_field_mapping(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        resp = MagicMock(status_code=200, json=lambda: _finnhub_payload())
        resp.raise_for_status = lambda: None
        with patch("requests.get", return_value=resp) as g:
            out = fb.fetch_overview_finnhub("bac")
        assert out["symbol"] == "BAC"
        assert out["PERatio"] == pytest.approx(13.29)
        assert out["DividendYield"] == pytest.approx(0.03229)      # percent -> fraction
        assert out["MarketCapitalization"] == pytest.approx(4.4736106e11)  # $M -> $
        assert out["Beta"] == pytest.approx(1.222)
        # D/E must use the long-term-debt variant, never the total-debt one
        assert out["DebtEquity"] == pytest.approx(0.9103)
        assert out["QuarterlyEarningsGrowthYOY"] == pytest.approx(0.3376)
        assert out["QuarterlyRevenueGrowthYOY"] == pytest.approx(0.7137)
        assert out["Volume"] == pytest.approx(33.79e6)
        assert out["ExDividendDate"] is None
        assert out["source"] == "finnhub_metric"
        # token goes in params, never in a logged URL
        assert g.call_args.kwargs["params"]["token"] == "fh-test-key"

    def test_http_error_returns_empty(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        with patch("requests.get", side_effect=ConnectionError("proxy reset")):
            assert fb.fetch_overview_finnhub("BAC") == {}

    def test_403_returns_empty(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key")
        resp = MagicMock(status_code=403)
        with patch("requests.get", return_value=resp):
            assert fb.fetch_overview_finnhub("BAC") == {}


# --------------------------------------------------------------------------
# Alpaca bars fetcher
# --------------------------------------------------------------------------

def _alpaca_payload(n=3):
    return {"bars": [
        {"t": f"2026-08-2{i}T04:00:00Z", "c": 60.0 + i, "v": 1_000_000 + i}
        for i in range(n)
    ]}


class TestAlpacaBars:
    def test_parsing_and_auth_headers(self, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY", "ak-test")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "sk-test")
        resp = MagicMock(status_code=200, json=lambda: _alpaca_payload())
        resp.raise_for_status = lambda: None
        with patch("requests.get", return_value=resp) as g:
            bars = fb.fetch_daily_bars_alpaca("BAC", days=300)
        assert [b["close"] for b in bars] == [60.0, 61.0, 62.0]
        assert bars[0]["volume"] == 1_000_000
        assert bars[0]["date"] == "2026-08-20"
        headers = g.call_args.kwargs["headers"]
        assert headers == {"APCA-API-KEY-ID": "ak-test", "APCA-API-SECRET-KEY": "sk-test"}
        # explicit start date required - without it Alpaca returns only today's bar
        assert g.call_args.kwargs["params"]["start"] <= date.today().isoformat()
        assert g.call_args.kwargs["params"]["feed"] == "iex"

    def test_http_error_returns_empty(self, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY", "ak-test")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "sk-test")
        with patch("requests.get", side_effect=ConnectionError("proxy reset")):
            assert fb.fetch_daily_bars_alpaca("BAC") == []


# --------------------------------------------------------------------------
# Wiring: fundamentals build_cache (Alpha -> Finnhub -> stale)
# --------------------------------------------------------------------------

@pytest.fixture
def isolated_fund_cache(monkeypatch, tmp_path):
    import core.fundamentals as f
    monkeypatch.setattr(f, "CACHE_FILE", tmp_path / "fundamentals_cache.json")
    monkeypatch.setattr(f.time, "sleep", lambda *_: None)
    return f


class TestFundamentalsFallback:
    def test_alpha_down_uses_finnhub(self, isolated_fund_cache, monkeypatch):
        f = isolated_fund_cache
        monkeypatch.setattr(f, "fetch_overview_alpha", lambda s: {})
        monkeypatch.setattr(f, "fetch_balance_sheet_alpha", lambda s: {})
        monkeypatch.setattr(f, "fetch_overview_finnhub",
                            lambda s: dict(_finnhub_mapped(s)) if s == "BAC" else {})
        out = f.build_cache(["BAC"])
        assert out["BAC"]["source"] == "finnhub_metric"
        assert out["BAC"]["DebtEquity"] == pytest.approx(0.9103)
        # fallback data persisted in the same cache schema
        cached = json.loads(f.CACHE_FILE.read_text())
        assert cached["fundamentals"][0]["source"] == "finnhub_metric"

    def test_alpha_wins_when_healthy(self, isolated_fund_cache, monkeypatch):
        f = isolated_fund_cache
        monkeypatch.setattr(f, "fetch_overview_alpha", lambda s: {"Symbol": s, "PERatio": "10"})
        monkeypatch.setattr(f, "fetch_balance_sheet_alpha", lambda s: {"DebtEquity": 0.5, "totalDebt": 100})
        fb_mock = MagicMock(side_effect=AssertionError("fallback must not be called"))
        monkeypatch.setattr(f, "fetch_overview_finnhub", fb_mock)
        out = f.build_cache(["BAC"])
        assert out["BAC"]["source"] == "alpha_overview"
        assert out["BAC"]["DebtEquity"] == 0.5
        fb_mock.assert_not_called()

    def test_both_down_uses_stale_cache(self, isolated_fund_cache, monkeypatch):
        f = isolated_fund_cache
        f.CACHE_FILE.write_text(json.dumps({
            "_timestamp": 1,  # ancient -> not TTL-valid, only stale
            "fundamentals": [{"symbol": "BAC", "PERatio": "9", "source": "alpha_overview"}],
        }))
        monkeypatch.setattr(f, "fetch_overview_alpha", lambda s: {})
        monkeypatch.setattr(f, "fetch_overview_finnhub", lambda s: {})
        monkeypatch.setattr(f, "fetch_balance_sheet_alpha", lambda s: {})
        out = f.build_cache(["BAC"])
        assert out["BAC"]["PERatio"] == "9"  # stale served, cache NOT overwritten
        assert json.loads(f.CACHE_FILE.read_text())["_timestamp"] == 1


def _finnhub_mapped(sym):
    return {
        "symbol": sym.upper(), "PERatio": 13.29, "DividendYield": 0.032,
        "MarketCapitalization": 4.47e11, "ProfitMargin": 0.26, "Volume": 3.3e7,
        "ROE": 0.095, "Beta": 1.22, "Sector": None, "AnalystTargetPrice": 55.0,
        "ExDividendDate": None, "QuarterlyEarningsGrowthYOY": 0.337,
        "QuarterlyRevenueGrowthYOY": 0.71, "DebtEquity": 0.9103,
        "totalDebt": None, "source": "finnhub_metric",
    }


# --------------------------------------------------------------------------
# Wiring: volatility build_cache (Alpha -> Alpaca bars -> stale/default)
# --------------------------------------------------------------------------

@pytest.fixture
def isolated_vol_cache(monkeypatch, tmp_path):
    import core.volatility as v
    monkeypatch.setattr(v, "CACHE_FILE", tmp_path / "volatility_cache.json")
    monkeypatch.setattr(v.time, "sleep", lambda *_: None)
    return v


def _fake_closes(n=120):
    return [50.0 + (i % 7) * 0.5 for i in range(n)]


class TestVolatilityFallback:
    def test_alpha_down_uses_alpaca(self, isolated_vol_cache, monkeypatch):
        v = isolated_vol_cache
        monkeypatch.setattr(v, "fetch_daily_alpha", lambda s, days=300: [])
        monkeypatch.setattr(v, "fetch_daily_bars_alpaca",
                            lambda s, days=450: [{"date": "2026-01-01", "close": c, "volume": 1}
                                                 for c in _fake_closes()])
        out = v.build_cache(["BAC"])
        assert out["BAC"]["source"] == "alpaca_bars"
        assert out["BAC"]["closes_count"] == 120
        cached = json.loads(v.CACHE_FILE.read_text())
        assert cached["volatility"][0]["source"] == "alpaca_bars"

    def test_both_down_no_cache_write(self, isolated_vol_cache, monkeypatch):
        v = isolated_vol_cache
        monkeypatch.setattr(v, "fetch_daily_alpha", lambda s, days=300: [])
        monkeypatch.setattr(v, "fetch_daily_bars_alpaca", lambda s, days=450: [])
        out = v.build_cache(["BAC"])
        assert out["BAC"]["source"] == "default"
        assert out["BAC"]["iv_rank_proxy"] == 50.0
        assert not v.CACHE_FILE.exists()  # default-only map must not poison cache

    def test_alpaca_entries_count_as_real_stale(self, isolated_vol_cache, monkeypatch):
        v = isolated_vol_cache
        v.CACHE_FILE.write_text(json.dumps({
            "_timestamp": 1,
            "volatility": [{"symbol": "BAC", "rv_20d": 22.0, "rv_60d": 20.0,
                            "iv_rank_proxy": 61.0, "closes_count": 300,
                            "source": "alpaca_bars"}],
        }))
        monkeypatch.setattr(v, "fetch_daily_alpha", lambda s, days=300: [])
        monkeypatch.setattr(v, "fetch_daily_bars_alpaca", lambda s, days=450: [])
        out = v.build_cache(["BAC"])
        assert out["BAC"]["iv_rank_proxy"] == 61.0  # stale alpaca entry kept, not defaulted


# --------------------------------------------------------------------------
# Wiring: liquidity evaluate_liquidity (Alpha -> Alpaca bars)
# --------------------------------------------------------------------------

class TestLiquidityFallback:
    def test_alpha_down_uses_alpaca(self, monkeypatch, tmp_path):
        import core.liquidity as liq
        monkeypatch.setattr(liq, "CACHE_FILE", tmp_path / "liquidity_cache.json")
        monkeypatch.setattr(liq.time, "sleep", lambda *_: None)
        monkeypatch.setattr(liq, "fetch_daily_volume_alpha", lambda s, days=30: [])
        vols = [2_000_000] * 25 + [900_000] * 5  # drying volume -> score penalty
        monkeypatch.setattr(liq, "fetch_daily_bars_alpaca",
                            lambda s, days=45: [{"date": "d", "close": 1, "volume": v} for v in vols])
        out = liq.evaluate_liquidity("BAC")
        assert out["avg_5d"] == pytest.approx(900_000)
        assert out["avg_20d"] == pytest.approx(2_000_000 * 20 / 20, rel=0.2)
        assert not out["trend_ok"]  # alpaca data actually drove the trend check
        cached = json.loads(liq.CACHE_FILE.read_text())
        assert cached["vol_history"]["BAC"]["vols"] == vols
