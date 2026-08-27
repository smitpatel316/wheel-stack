"""
Earnings-source pull, fail-open (Pi migration, 2026-08-27).

Background: when the Finnhub earnings webhook receiver ran on this host, it
cleared logs/earnings_cache.json on every Finnhub push event so the next
strategy run refetched fresh dates. The receiver is moving to the Pi, so
those invalidation signals no longer reach the Hatch-local cache. Instead,
the engine now PULLS the receiver's invalidation state at run start:

    GET {EARNINGS_SOURCE_URL}/earnings/state   (<=5s timeout)
    -> {"last_invalidation": <epoch>, "events_received": n, ...}

If the receiver's last invalidation is newer than the local cache (and newer
than the last invalidation we already applied), the local earnings cache is
cleared so the current run refetches from Finnhub, and the applied marker is
persisted to state/earnings-source-state.json.

FAIL-OPEN CONTRACT: ANY failure — unset URL, connection error, timeout, bad
JSON, non-2xx, unwritable state — logs a loud, grep-able WARNING
("[EARNINGS-SOURCE]") and leaves the local cache untouched. The run then
continues with exactly today's behavior: fresh cache -> 48h stale cache ->
state/earnings-last-good.json snapshot -> Alpha Vantage fallback. This
function NEVER raises. With EARNINGS_SOURCE_URL unset (the default) it is a
complete no-op: zero behavior change on the all-localhost setup.

Env:
    EARNINGS_SOURCE_URL        base URL of the webhook receiver (default unset = off)
    EARNINGS_SOURCE_TIMEOUT    pull timeout seconds (default 5)
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("strategy.earnings_source")

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state" / "earnings-source-state.json"
STATE_PATH = "/earnings/state"


def source_url() -> str:
    return (os.getenv("EARNINGS_SOURCE_URL") or "").rstrip("/")


def pull_timeout() -> float:
    try:
        return float(os.getenv("EARNINGS_SOURCE_TIMEOUT", "5"))
    except ValueError as e:
        logger.debug("[SWALLOWED] EARNINGS_SOURCE_TIMEOUT not a number, using 5s: %r", e)
        return 5.0


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _json_timestamp(path: Path, key: str) -> float:
    try:
        return float(json.loads(path.read_text()).get(key) or 0)
    except Exception as e:
        logger.debug("[SWALLOWED] timestamp read %s[%s] failed (treating as 0): %r", path, key, e)
        return 0.0


def sync_from_source(cache_file: Optional[Path] = None, state_file: Optional[Path] = None) -> bool:
    """Pull invalidation state; clear the local earnings cache if stale.

    Returns True if the receiver was reached (whether or not a clear was
    needed), False if the pull failed or the feature is off. Never raises.
    """
    url = source_url()
    if not url:
        return False  # feature off - identical to pre-migration behavior
    if cache_file is None:
        from core.earnings_calendar import CACHE_FILE
        cache_file = CACHE_FILE
    state_file = state_file or STATE_FILE
    try:
        r = requests.get(f"{url}{STATE_PATH}", timeout=pull_timeout())
        if r.status_code != 200:
            logger.warning("[EARNINGS-SOURCE] pull from %s returned HTTP %s - "
                           "keeping local earnings cache (fail-open, run continues)", url, r.status_code)
            return False
        remote_ts = float((r.json() or {}).get("last_invalidation") or 0)
    except Exception as e:
        # Exception type only - never repr(e), the URL could carry internals.
        logger.warning("[EARNINGS-SOURCE] pull from %s failed (%s) - "
                       "keeping local earnings cache (fail-open, run continues)", url, type(e).__name__)
        return False

    if remote_ts <= 0:
        logger.info("[EARNINGS-SOURCE] receiver at %s reports no invalidations yet - local cache unchanged", url)
        return True
    local_ts = _json_timestamp(cache_file, "_timestamp")
    applied_ts = _json_timestamp(state_file, "last_applied_invalidation")
    if remote_ts <= max(local_ts, applied_ts):
        logger.info("[EARNINGS-SOURCE] local earnings cache already current "
                    "(remote invalidation %s)", time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(remote_ts)))
        return True
    # The receiver saw a newer Finnhub event than our cache: clear so this
    # run refetches from Finnhub (with stale/last-good/Alpha fallbacks).
    try:
        if cache_file.exists():
            cache_file.unlink()
            logger.info("[EARNINGS-SOURCE] remote invalidation %s newer than local cache - "
                        "cleared %s, this run refetches from Finnhub",
                        time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(remote_ts)), cache_file)
        else:
            logger.info("[EARNINGS-SOURCE] remote invalidation %s noted, no local cache to clear",
                        time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(remote_ts)))
        _atomic_write(state_file, json.dumps({"last_applied_invalidation": remote_ts}, indent=2))
        return True
    except Exception as e:
        logger.warning("[EARNINGS-SOURCE] cache clear after remote invalidation failed (%s) - "
                       "keeping old cache (fail-open, run continues)", type(e).__name__)
        return False
