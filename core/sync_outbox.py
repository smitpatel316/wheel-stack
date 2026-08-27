"""
Fail-open Optionable sync outbox (Pi migration, 2026-08-27).

CANONICAL STATE: the engine journal (app_logging output + state/*.json) and
the Alpaca broker are the source of truth. The Optionable dashboard is ONLY
a replica for viewing. A dashboard outage (Pi down, tunnel flapping, Hatch
service wipe) must therefore never halt, meaningfully slow, or invalidate a
strategy run — and it must not silently lose a trade record either.

Design: every state-changing payload the engine sends to Optionable (today:
new trade records from fills/rolls via optionable_sync.push_trade_to_optionable)
is written FIRST to a durable local outbox directory — one JSON file per
payload, atomic tmp+rename — and only then delivered over HTTP. Delivery
uses a short (<=5s) timeout and can NEVER raise into the engine: every
failure is logged loudly ([SYNC] WARNING) and the item stays queued.

Draining: at engine run start (and after successful end-of-run syncs) the
outbox drains oldest-first. An item is deleted only after a 2xx
acknowledgement — or when the receiver provably already has it.

Idempotency: each trade payload carries a stable sync id (uuid5 of OCC
symbol + opened date) in the trade's notes. Before POSTing, the drainer
GETs the receiver's trades and treats a matching syncId (or the legacy
ticker/strike/expiry/type tuple match) as already-recorded, so re-delivery
after a crash does not double-record. This works against Optionable's
existing API — no receiver changes required.

Scope note: the end-of-run reconciliation syncs (equity / SGOV /
closed-trades / activities) and the per-run dashboard telemetry push are
deliberately NOT routed through the outbox. They are recomputed from
canonical broker state every run, so a missed run self-heals on the next
one, and replaying a stale snapshot hours later would actively corrupt the
dashboard's current view. Only event-style writes (a fill happened — record
it) need durability.

Env:
    OPTIONABLE_URL      dashboard base URL (default http://localhost:8096)
    SYNC_OUTBOX_DIR     outbox location (default <repo>/state/sync-outbox)
    SYNC_PUSH_TIMEOUT   delivery timeout seconds (default 5)
"""
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("strategy.sync_outbox")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTBOX_DIR = ROOT / "state" / "sync-outbox"
MAX_DRAIN_PER_RUN = 50


def outbox_dir() -> Path:
    return Path(os.getenv("SYNC_OUTBOX_DIR") or str(DEFAULT_OUTBOX_DIR))


def push_timeout() -> float:
    try:
        return float(os.getenv("SYNC_PUSH_TIMEOUT", "5"))
    except ValueError as e:
        logger.debug("[SWALLOWED] SYNC_PUSH_TIMEOUT not a number, using 5s: %r", e)
        return 5.0


def base_url() -> str:
    return (os.getenv("OPTIONABLE_URL") or "http://localhost:8096").rstrip("/")


def make_trade_sync_id(occ_symbol: str, opened_date: str) -> str:
    """Stable id for a trade payload: same fill re-enqueued = same id."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"wheel-stack:trade:{occ_symbol}:{opened_date}"))


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _find_by_id(sync_id: str) -> Optional[Path]:
    try:
        d = outbox_dir()
        if not d.exists():
            return None
        for f in sorted(d.glob("*.json")):
            try:
                if json.loads(f.read_text()).get("id") == sync_id:
                    return f
            except Exception as e:
                logger.debug("[SWALLOWED] outbox scan of %s failed: %r", f.name, e)
                continue
    except Exception as e:
        logger.debug("[SWALLOWED] outbox scan failed: %r", e)
    return None


def outbox_pending() -> int:
    try:
        d = outbox_dir()
        return len(list(d.glob("*.json"))) if d.exists() else 0
    except Exception as e:
        logger.debug("[SWALLOWED] outbox pending count failed: %r", e)
        return 0


def is_queued(sync_id: str) -> bool:
    return _find_by_id(sync_id) is not None


def enqueue(kind: str, payload: dict, sync_id: str, path: str = "", method: str = "POST") -> Optional[Path]:
    """Write a payload to the durable outbox. Never raises.

    Idempotent: if an item with the same sync id is already queued, the
    existing file is kept (first-write wins) and its path returned.
    Returns the outbox file path, or None if the write itself failed.
    """
    try:
        existing = _find_by_id(sync_id)
        if existing is not None:
            logger.debug("[SYNC] outbox already holds %s (%s) - first write wins", sync_id[:8], kind)
            return existing
        item = {
            "id": sync_id,
            "kind": kind,
            "method": method,
            "path": path,
            "payload": payload,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
            "attempts": 0,
        }
        dest = outbox_dir() / f"{int(time.time() * 1000):013d}-{sync_id}.json"
        _atomic_write(dest, json.dumps(item, indent=2))
        return dest
    except Exception as e:
        logger.warning("[SYNC] OUTBOX WRITE FAILED for %s (%s) - payload NOT durable: %r", sync_id, kind, e)
        return None


def enqueue_trade(payload: dict, sync_id: str) -> Optional[Path]:
    return enqueue("trade", payload, sync_id, path="/api/trades", method="POST")


# ---------------- delivery ----------------

def _get_all_trades(base: str, account_id: int):
    """List of receiver trades, or None if the server couldn't be read."""
    try:
        r = requests.get(f"{base}/api/trades", params={"accountId": account_id}, timeout=push_timeout())
        if r.status_code == 200:
            return r.json().get("data") or []
        logger.warning("[SYNC] Optionable GET /api/trades HTTP %s during outbox drain", r.status_code)
        return None
    except Exception as e:
        logger.warning("[SYNC] Optionable unreachable during outbox drain (%s): %s", base, type(e).__name__)
        return None


def _trade_already_recorded(trades, payload: dict, sync_id: str) -> bool:
    for t in trades:
        try:
            if sync_id and sync_id in str(t.get("notes") or ""):
                return True
            if (t.get("ticker") == payload.get("ticker")
                    and t.get("type") == payload.get("type")
                    and t.get("expirationDate") == payload.get("expirationDate")
                    and abs(float(t.get("strike", 0)) - float(payload.get("strike", 0))) < 0.001):
                return True
        except Exception as e:
            logger.debug("[SWALLOWED] outbox dedupe compare failed for trade id %s: %r", t.get("id"), e)
            continue
    return False


def _deliver(item: dict, base: str) -> str:
    """One delivery attempt. Returns 'acked' | 'retry' | 'down'. Never raises."""
    kind = item.get("kind")
    sync_id = str(item.get("id") or "")
    payload = item.get("payload") or {}
    try:
        if kind == "trade":
            trades = _get_all_trades(base, int(payload.get("accountId") or 1))
            if trades is None:
                return "down"
            if _trade_already_recorded(trades, payload, sync_id):
                logger.info("[SYNC] outbox item %s already recorded in Optionable (idempotent skip)", sync_id[:8])
                return "acked"
        r = requests.request(item.get("method", "POST"), f"{base}{item.get('path') or '/api/trades'}",
                             json=payload, timeout=push_timeout())
        if r.status_code in (200, 201):
            return "acked"
        txt = (r.text or "")[:800]
        if r.status_code in (400, 409) and any(w in txt.lower() for w in ("already", "duplicate", "exists")):
            return "acked"
        logger.warning("[SYNC] outbox delivery HTTP %s for %s (%s): %s - kept queued",
                       r.status_code, sync_id[:8], kind, txt[:200])
        return "retry"
    except Exception as e:
        # Exception type only - never repr(e), URLs/messages can carry internals.
        logger.warning("[SYNC] outbox delivery failed for %s (%s): %s - kept queued",
                       sync_id[:8], kind, type(e).__name__)
        return "down"


def drain_outbox(base: Optional[str] = None, max_items: int = MAX_DRAIN_PER_RUN) -> dict:
    """Deliver queued payloads oldest-first; delete only after ack. Never raises.

    Stops at the first 'down' (server unreachable) result — the rest of the
    queue would fail the same way and each attempt costs a timeout.
    """
    stats = {"pending": 0, "delivered": 0, "kept": 0, "down": False}
    try:
        d = outbox_dir()
        if not d.exists():
            return stats
        files = sorted(d.glob("*.json"))
        stats["pending"] = len(files)
        if not files:
            return stats
        base = (base or base_url()).rstrip("/")
        for f in files[:max_items]:
            try:
                item = json.loads(f.read_text())
            except Exception as e:
                logger.warning("[SYNC] corrupt outbox item %s (%r) - quarantining to .bad", f.name, e)
                try:
                    os.replace(f, f.with_suffix(".bad"))
                except Exception as e2:
                    logger.warning("[SYNC] quarantine of corrupt outbox item %s failed: %r", f.name, e2)
                continue
            result = _deliver(item, base)
            if result == "acked":
                stats["delivered"] += 1
                try:
                    f.unlink()
                except Exception as e:
                    # Deletion failed after ack: next drain's dedupe check
                    # (syncId / tuple match) treats it as already-recorded.
                    logger.warning("[SYNC] acked outbox item %s could not be deleted "
                                   "(idempotent re-check will clear it next run): %r", f.name, e)
            elif result == "down":
                stats["down"] = True
                stats["kept"] = stats["pending"] - stats["delivered"]
                logger.warning("[SYNC] Optionable down/unreachable - outbox drain aborted, "
                               "%d item(s) still queued (fail-open: engine run unaffected)", stats["kept"])
                break
            else:  # retry
                stats["kept"] += 1
                try:
                    item["attempts"] = int(item.get("attempts") or 0) + 1
                    _atomic_write(f, json.dumps(item, indent=2))
                except Exception as e:
                    logger.debug("[SWALLOWED] outbox attempts bump failed for %s: %r", f.name, e)
        if stats["delivered"] or stats["kept"]:
            logger.info("[SYNC] outbox drain: %d delivered/confirmed, %d still queued (pending was %d)",
                        stats["delivered"], stats["kept"], stats["pending"])
    except Exception as e:
        logger.warning("[SYNC] outbox drain failed (fail-open, engine unaffected): %r", e)
    return stats
