"""Earnings-calendar fetch failure vs. empty-calendar behavior (2026-08-17).

Why: fetch_earnings_finnhub used to return [] for BOTH "request failed" and
"genuinely empty calendar", and the Alpha fallback in build_cache was a
no-op `pass`. A Finnhub outage + expired cache therefore silently disabled
earnings blocking. Now failure returns None, stale cache is retained, and
Alpha EARNINGS_CALENDAR (the only Alpha endpoint with future dates) is the
last resort; total absence of data logs a DEGRADED warning.
"""

import json
import time
from datetime import date, datetime, timedelta

import core.earnings_calendar as ec


class _Resp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(ec, "CACHE_FILE", tmp_path / "earnings_cache.json")


def test_finnhub_failure_returns_none_not_empty(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(status=503))
    assert ec.fetch_earnings_finnhub(date.today(), date.today()) is None


def test_finnhub_empty_calendar_returns_empty_list(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(payload={"earningsCalendar": []}))
    assert ec.fetch_earnings_finnhub(date.today(), date.today()) == []


def test_finnhub_429_exhaustion_returns_none(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(status=429))
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert ec.fetch_earnings_finnhub(date.today(), date.today()) is None


def test_failure_retains_stale_cache(monkeypatch, tmp_path):
    cache = tmp_path / "earnings_cache.json"
    monkeypatch.setattr(ec, "CACHE_FILE", cache)
    future = (date.today() + timedelta(days=10)).isoformat()
    cache.write_text(json.dumps({
        "_timestamp": time.time() - 24 * 3600,  # past 6h TTL, within 48h stale-OK
        "earningsCalendar": [{"symbol": "AAA", "date": future}],
    }))
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(status=503))
    m = ec.build_cache(["AAA"])
    assert m.get("AAA") == date.today() + timedelta(days=10)


def test_failure_no_cache_falls_back_to_alpha_calendar(monkeypatch, tmp_path):
    _no_cache(monkeypatch, tmp_path)
    future = (date.today() + timedelta(days=20)).isoformat()
    csv_text = f"symbol,name,reportDate,fiscalDateEnding,estimate,currency\nAAA,AAA Inc,{future},,0.5,USD\nZZZ,Other,{future},,0.1,USD\n"

    def fake_get(url, params=None, timeout=None):
        if "finnhub" in url:
            return _Resp(status=503)
        assert params["function"] == "EARNINGS_CALENDAR"
        return _Resp(text=csv_text)

    monkeypatch.setattr("requests.get", fake_get)
    m = ec.build_cache(["AAA"])
    assert m.get("AAA") == date.today() + timedelta(days=20)
    # cache file written so subsequent runs don't hammer Alpha
    saved = json.loads((tmp_path / "earnings_cache.json").read_text())
    assert saved["earningsCalendar"][0]["symbol"] == "AAA"


def test_empty_calendar_success_does_not_fall_back(monkeypatch, tmp_path):
    _no_cache(monkeypatch, tmp_path)
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        if "finnhub" in url:
            return _Resp(payload={"earningsCalendar": []})
        raise AssertionError("Alpha must not be called on a successful empty Finnhub calendar")

    monkeypatch.setattr("requests.get", fake_get)
    m = ec.build_cache(["AAA"])
    assert m == {}
    assert len(calls) == 1


def test_total_data_loss_logs_degraded(monkeypatch, tmp_path, capsys):
    _no_cache(monkeypatch, tmp_path)
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(status=503))
    ec.build_cache(["AAA"])
    assert "DEGRADED" in capsys.readouterr().out


def test_alpha_calendar_parser_ignores_past_and_duplicates(monkeypatch):
    past = (date.today() - timedelta(days=5)).isoformat()
    d1 = (date.today() + timedelta(days=30)).isoformat()
    d2 = (date.today() + timedelta(days=10)).isoformat()
    csv_text = (
        "symbol,name,reportDate,fiscalDateEnding,estimate,currency\n"
        f"AAA,AAA Inc,{past},,0.5,USD\n"
        f"AAA,AAA Inc,{d1},,0.5,USD\n"
        f"AAA,AAA Inc,{d2},,0.5,USD\n"
        "BBB,Bad,,,,\n"
    )
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(text=csv_text))
    monkeypatch.setattr(ec, "get_alpha_key", lambda: "k")
    out = ec.fetch_earnings_calendar_alpha(["AAA", "BBB"])
    assert out == {"AAA": datetime.fromisoformat(d2).date()}
