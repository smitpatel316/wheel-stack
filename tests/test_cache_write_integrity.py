"""Cache write-integrity regressions (data-pipeline swarm, 2026-10-12).

Sibling hunt for c22b0dd (fundamentals cache-hit refresh persisted only the
in-memory subset). Findings covered here:

1. The c22b0dd persist wrote ``list(stale.values())`` — only symbols in the
   current request — silently dropping every other cached symbol from disk.
   The persist must merge refreshed entries into the FULL on-disk list.
2. All JSON cache writes (fundamentals / volatility / dividend / earnings /
   liquidity) were plain ``write_text``: a run killed mid-write (container
   replacements are routine) leaves a truncated, unparseable cache. Writes
   are now tmp+rename (atomic); no ``.tmp`` residue may remain.
3. Liquidity's write path treated an unparseable existing cache as
   "starting fresh" and overwrote ALL symbols' history with one symbol.
   The corrupt file is now quarantined (``.corrupt-<epoch>``) first.
4. earnings_source logged "already current" at INFO even when the
   receiver's last_invalidation was weeks old (live: frozen 2026-08-28 for
   6+ weeks). An invalidation older than 7 days now logs a WARNING.
"""
import json
import logging
import time
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# 1. fundamentals cache-hit persist preserves non-requested symbols
# ---------------------------------------------------------------------------

def test_fund_cache_hit_persist_preserves_unrequested_symbols(tmp_path, monkeypatch):
    import core.fundamentals as f

    cache = tmp_path / "fundamentals_cache.json"
    monkeypatch.setattr(f, "CACHE_FILE", cache)
    monkeypatch.setattr(f, "LOG_DIR", tmp_path)
    monkeypatch.setattr(f.time, "sleep", lambda *_: None)
    ts = time.time()
    cache.write_text(json.dumps({
        "_timestamp": ts,
        "fundamentals": [
            {"symbol": "AAA", "PERatio": "30", "DebtEquity": 0.1, "source": "finnhub_metric"},
            {"symbol": "ZZZ", "PERatio": "9", "DebtEquity": 0.4, "source": "alpha_overview"},
        ],
    }))
    monkeypatch.setattr(f, "_fetch_primary_entry",
                        lambda s: {"symbol": "AAA", "PERatio": "31", "DebtEquity": 2.5,
                                   "source": "alpha_overview"})
    out = f.build_cache(["AAA"])
    assert out["AAA"]["DebtEquity"] == 2.5  # refreshed in memory (c22b0dd behavior)
    on_disk = json.loads(cache.read_text())
    by_sym = {e["symbol"]: e for e in on_disk["fundamentals"]}
    assert by_sym["AAA"]["source"] == "alpha_overview"  # refresh persisted
    assert by_sym["ZZZ"]["PERatio"] == "9"  # unrequested entry NOT dropped
    assert on_disk["_timestamp"] == ts  # original TTL preserved
    assert not (tmp_path / "fundamentals_cache.json.tmp").exists()


def test_fund_rebuild_write_is_atomic(tmp_path, monkeypatch):
    import core.fundamentals as f

    cache = tmp_path / "fundamentals_cache.json"
    monkeypatch.setattr(f, "CACHE_FILE", cache)
    monkeypatch.setattr(f, "LOG_DIR", tmp_path)
    monkeypatch.setattr(f.time, "sleep", lambda *_: None)
    monkeypatch.setattr(f, "fetch_overview_alpha", lambda s: {"Symbol": s, "PERatio": "10"})
    monkeypatch.setattr(f, "fetch_balance_sheet_alpha", lambda s: {"DebtEquity": 0.5, "totalDebt": 1})
    monkeypatch.setattr(f, "fetch_overview_finnhub", lambda s: {})
    out = f.build_cache(["AAA"])
    assert out["AAA"]["DebtEquity"] == 0.5
    assert json.loads(cache.read_text())["fundamentals"][0]["symbol"] == "AAA"
    assert not (tmp_path / "fundamentals_cache.json.tmp").exists()


# ---------------------------------------------------------------------------
# 2/3. liquidity: atomic write + corrupt-file quarantine
# ---------------------------------------------------------------------------

def test_liquidity_corrupt_cache_quarantined_not_silently_wiped(tmp_path, monkeypatch):
    import core.liquidity as liq

    cache = tmp_path / "liquidity_cache.json"
    monkeypatch.setattr(liq, "CACHE_FILE", cache)
    monkeypatch.setattr(liq, "LOG_DIR", tmp_path)
    monkeypatch.setattr(liq.time, "sleep", lambda *_: None)
    cache.write_text("{not json - truncated by a killed run")
    monkeypatch.setattr(liq, "fetch_daily_volume_alpha", lambda s, days=30: [1_000_000] * 25)
    out = liq.evaluate_liquidity("BAC")
    assert out["avg_20d"] == pytest.approx(1_000_000)
    quarantined = list(tmp_path.glob("liquidity_cache.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "{not json - truncated by a killed run"
    # new cache written atomically, parseable, no tmp residue
    assert json.loads(cache.read_text())["vol_history"]["BAC"]["vols"] == [1_000_000] * 25
    assert not (tmp_path / "liquidity_cache.json.tmp").exists()


def test_liquidity_write_preserves_other_symbols(tmp_path, monkeypatch):
    import core.liquidity as liq

    cache = tmp_path / "liquidity_cache.json"
    monkeypatch.setattr(liq, "CACHE_FILE", cache)
    monkeypatch.setattr(liq, "LOG_DIR", tmp_path)
    monkeypatch.setattr(liq.time, "sleep", lambda *_: None)
    cache.write_text(json.dumps({"vol_history": {"AAA": {"vols": [5] * 25, "_ts": 1}}}))
    monkeypatch.setattr(liq, "fetch_daily_volume_alpha", lambda s, days=30: [1_000_000] * 25)
    liq.evaluate_liquidity("BAC")
    raw = json.loads(cache.read_text())
    assert set(raw["vol_history"]) == {"AAA", "BAC"}


# ---------------------------------------------------------------------------
# 4. earnings_source: ancient invalidation warns instead of reading healthy
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def test_earnings_source_ancient_invalidation_warns(tmp_path, monkeypatch, caplog):
    import core.earnings_source as es

    monkeypatch.setenv("EARNINGS_SOURCE_URL", "https://example.invalid")
    cache = tmp_path / "earnings_cache.json"
    state = tmp_path / "earnings-source-state.json"
    old_ts = time.time() - 45 * 86400
    with mock.patch.object(es.requests, "get",
                           return_value=_Resp({"last_invalidation": old_ts})):
        with caplog.at_level(logging.WARNING, logger="strategy.earnings_source"):
            assert es.sync_from_source(cache_file=cache, state_file=state) is True
    assert any("last invalidation is" in r.message and "days old" in r.message
               for r in caplog.records)


def test_earnings_source_fresh_invalidation_no_stale_warning(tmp_path, monkeypatch, caplog):
    import core.earnings_source as es

    monkeypatch.setenv("EARNINGS_SOURCE_URL", "https://example.invalid")
    cache = tmp_path / "earnings_cache.json"
    state = tmp_path / "earnings-source-state.json"
    recent_ts = time.time() - 3600
    cache.write_text(json.dumps({"_timestamp": time.time()}))
    with mock.patch.object(es.requests, "get",
                           return_value=_Resp({"last_invalidation": recent_ts})):
        with caplog.at_level(logging.WARNING, logger="strategy.earnings_source"):
            assert es.sync_from_source(cache_file=cache, state_file=state) is True
    assert not any("days old" in r.message for r in caplog.records)
