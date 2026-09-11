"""Persistent order-intent ledger for the Robinhood live order path.

Why this exists: place_option_order() derives its server-side idempotency key
as uuid5(UUID5_NS, logical_key). That dedupes a *retried identical key*, but it
cannot answer the questions that matter after a crash or across runs:

  - "Did the order I just sent actually land?" (response lost)
  - "Did a previous run already place this exact trade?" (restart / retry slot)
  - "Are these two intents the same trade, or two different trades?"
    (the old key collapsed limit@$1.66 and market into one ref_id)

The ledger is the engine-side system of record. Every placement goes through
it; every state transition is a SQLite transaction (WAL + FULL synchronous),
so a crash between any two steps leaves a recoverable row instead of a guess.

State machine:
    INTENDED --review blocked--> REJECTED_BY_REVIEW (terminal)
             --dry run---------> DRY_RUN (terminal, audit only)
             --send attempted--> SENDING --place raised--> PLACED_UNCONFIRMED
                                          --placed ok----> PLACED
             --place raised---> PLACED_UNCONFIRMED --reconcile--> PLACED
                                                         \\--> NEEDS_REVIEW (terminal-ish,
                                                                contract blocked)
             --placed ok------> PLACED --poll--> FILLED | CANCELLED | REJECTED
    SENDING is the crash-safety Rubicon: it is written immediately BEFORE the
    transport send. A previous run's INTENDED row (SENDING never written)
    provably never reached the send and is safe to STALE; a previous run's
    SENDING row may have reached the broker and is promoted to
    PLACED_UNCONFIRMED by reconcile() -- never STALEd.
    Any INTENDED row from a previous run_id is marked STALE when a new run
    begins an intent. UNKNOWN is set when the broker cannot be read during
    reconcile: fail closed, no new placements until reconcile succeeds.

Fail-closed rules:
  - begin_intent() raises IntentBlockedError if the same contract+side+effect
    has a PLACED_UNCONFIRMED / NEEDS_REVIEW / UNKNOWN intent, or any other
    open intent with different terms (price/qty). One open intent per
    contract+side+effect: the engine must cancel (or a human must resolve)
    the working order before re-intending. A human resolves via the CLI below.
  - reconcile() raises IntentBlockedError (and marks rows UNKNOWN) when the
    broker order list itself is unreadable. Trading blind is refused.
  - An unconfirmed intent that matches no broker order is NEEDS_REVIEW, never
    silently ABANDONED: absence from a list is not proof of absence.

The ledger never places, cancels, or reviews orders itself. It only records.
All broker I/O is injected (list_orders_fn) so tests never touch the network.

CLI (manual ops):
    PYTHONPATH=. python core/order_intent_ledger.py list [--state S]
    PYTHONPATH=. python core/order_intent_ledger.py show <id>
    PYTHONPATH=. python core/order_intent_ledger.py resolve <id> PLACED|ABANDONED
"""

import argparse
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# MUST equal rh_order_client.UUID5_NS (DNS namespace). place_option_order()
# derives ref_id = uuid5(UUID5_NS, logical_key); the ledger stores the same
# value so reconcile() can match broker orders by ref_id. A test pins this.
UUID5_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

# Non-terminal states: an intent in one of these still represents a live
# economic position the engine must not duplicate. SENDING (the send was
# attempted but the outcome is unrecorded) is the most dangerous: it may
# already be a live broker order.
OPEN_STATES = {"INTENDED", "SENDING", "PLACED", "PLACED_UNCONFIRMED", "UNKNOWN"}

# States that block a new intent for the same contract+side+effect until a
# human resolves them.
BLOCKING_STATES = {"PLACED_UNCONFIRMED", "NEEDS_REVIEW", "UNKNOWN"}

# Fallback reconcile match window when the broker order has no ref_id field.
RECONCILE_WINDOW_S = 900

SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    id              INTEGER PRIMARY KEY,
    intent_key      TEXT NOT NULL UNIQUE,
    ref_id          TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    occ_symbol      TEXT NOT NULL,
    side            TEXT NOT NULL,
    effect          TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    order_type      TEXT NOT NULL,
    limit_price     REAL,
    account_last4   TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL,
    broker_order_id TEXT,
    fill_qty        REAL,
    fill_avg_price  REAL,
    note            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_intents_state ON intents(state);
CREATE INDEX IF NOT EXISTS idx_intents_contract
    ON intents(occ_symbol, side, effect, state);
"""


def ref_id_for(intent_key: str) -> str:
    """Server-side idempotency key for an intent (mirrors place_option_order)."""
    return str(uuid.uuid5(UUID5_NAMESPACE, intent_key))


def _px_eq(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 1e-9


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class IntentBlockedError(RuntimeError):
    """A new intent was refused: an unresolved intent blocks this trade."""


@dataclass
class Intent:
    id: int
    intent_key: str
    ref_id: str
    run_id: str
    created_at: str
    occ_symbol: str
    side: str
    effect: str
    qty: int
    order_type: str
    limit_price: float | None
    account_last4: str
    state: str
    broker_order_id: str | None
    fill_qty: float | None
    fill_avg_price: float | None
    note: str


class OrderIntentLedger:
    """SQLite-backed record of every order the engine intended to place."""

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False + explicit lock: safe for the engine's
        # single-threaded use and for tests that share a ledger.
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------ internals
    @staticmethod
    def _row_to_intent(row: sqlite3.Row) -> Intent:
        return Intent(
            id=row["id"], intent_key=row["intent_key"], ref_id=row["ref_id"],
            run_id=row["run_id"], created_at=row["created_at"],
            occ_symbol=row["occ_symbol"], side=row["side"], effect=row["effect"],
            qty=row["qty"], order_type=row["order_type"],
            limit_price=row["limit_price"], account_last4=row["account_last4"],
            state=row["state"], broker_order_id=row["broker_order_id"],
            fill_qty=row["fill_qty"], fill_avg_price=row["fill_avg_price"],
            note=row["note"] or "",
        )

    def _get(self, intent_id: int) -> Intent | None:
        cur = self._conn.execute("SELECT * FROM intents WHERE id = ?", (intent_id,))
        row = cur.fetchone()
        return self._row_to_intent(row) if row else None

    def _set_state(self, intent_id: int, state: str, note: str = "",
                   broker_order_id: str | None = None,
                   fill_qty: float | None = None,
                   fill_avg_price: float | None = None) -> Intent:
        with self._lock:
            if broker_order_id is not None:
                self._conn.execute(
                    "UPDATE intents SET state=?, note=?, broker_order_id=?, "
                    "fill_qty=COALESCE(?, fill_qty), "
                    "fill_avg_price=COALESCE(?, fill_avg_price), "
                    "updated_at=? WHERE id=?",
                    (state, note, broker_order_id, fill_qty, fill_avg_price,
                     _utcnow(), intent_id))
            else:
                self._conn.execute(
                    "UPDATE intents SET state=?, note=?, "
                    "fill_qty=COALESCE(?, fill_qty), "
                    "fill_avg_price=COALESCE(?, fill_avg_price), "
                    "updated_at=? WHERE id=?",
                    (state, note, fill_qty, fill_avg_price, _utcnow(), intent_id))
            self._conn.commit()
            return self._get(intent_id)

    # ------------------------------------------------------------ intents
    @staticmethod
    def build_key(run_id: str, occ_symbol: str, side: str, effect: str,
                  qty: int, order_type: str, limit_price: float | None) -> str:
        """Enriched idempotency key.

        Unlike the old "{occ}:{side}:{effect}:{qty}" key this isolates runs
        (a fresh decision each run is a new intent) and distinguishes prices
        (limit@$1.66 vs market are different intents, not one ref_id).
        """
        px = f"{limit_price:.4f}" if limit_price is not None else "mkt"
        return (f"{run_id}:{occ_symbol.strip().upper()}:{side.lower()}:"
                f"{effect.lower()}:{int(qty)}:{order_type.lower()}:{px}")

    def begin_intent(self, *, run_id: str, occ_symbol: str, side: str,
                     effect: str, qty: int, order_type: str = "market",
                     limit_price: float | None = None,
                     account_last4: str = "") -> tuple[Intent, bool]:
        """Open (or adopt) an intent. Returns (intent, created).

        One open intent per (contract, side, effect): adopting an identical
        re-intent is idempotent, but a *different* price/qty while one is
        open raises IntentBlockedError -- the engine must cancel (or a human
        must resolve) the working order first. Two live orders for one trade
        is the failure this ledger exists to prevent.
        Raises IntentBlockedError when an unresolved intent blocks this trade.
        """
        occ = occ_symbol.strip().upper()
        side_l, effect_l = side.lower(), effect.lower()
        qty_i = int(qty)
        type_l = order_type.lower()
        px = float(limit_price) if limit_price is not None else None
        with self._lock:
            # Previous runs' never-sent intents are dead letters, not
            # blockers. This is sound ONLY because of the SENDING Rubicon:
            # any intent that reached the transport send was marked SENDING
            # first, so a previous run's INTENDED row provably never reached
            # the send. (Previous runs' SENDING rows are promoted by
            # reconcile(), never STALEd -- see _promote_interrupted_sends.)
            self._conn.execute(
                "UPDATE intents SET state='STALE', updated_at=? "
                "WHERE state='INTENDED' AND run_id != ?",
                (_utcnow(), run_id))
            blocker = self._conn.execute(
                "SELECT id, state FROM intents WHERE occ_symbol=? AND side=? "
                "AND effect=? AND state IN ('PLACED_UNCONFIRMED','NEEDS_REVIEW','UNKNOWN') "
                "ORDER BY id DESC LIMIT 1",
                (occ, side_l, effect_l)).fetchone()
            if blocker:
                self._conn.commit()
                raise IntentBlockedError(
                    f"trade {occ} {side_l}/{effect_l} blocked by intent "
                    f"#{blocker['id']} in state {blocker['state']}: resolve it "
                    f"first (core/order_intent_ledger.py resolve)")
            sending = self._conn.execute(
                "SELECT id, run_id FROM intents WHERE occ_symbol=? AND side=? "
                "AND effect=? AND state='SENDING' "
                "ORDER BY id DESC LIMIT 1",
                (occ, side_l, effect_l)).fetchone()
            if sending:
                # A previous run attempted this send and died before
                # recording the outcome. reconcile() (which the adapter runs
                # at startup before any begin_intent) promotes these -- if
                # you are here without reconciling, do that first.
                self._conn.commit()
                raise IntentBlockedError(
                    f"trade {occ} {side_l}/{effect_l} has intent "
                    f"#{sending['id']} in SENDING (run {sending['run_id'][:8]} "
                    f"may have placed it): run reconcile() first; the "
                    f"adapter does this automatically at startup")
            existing = self._conn.execute(
                "SELECT * FROM intents WHERE occ_symbol=? AND side=? AND effect=? "
                "AND state IN ('INTENDED','PLACED','PLACED_UNCONFIRMED','UNKNOWN') "
                "ORDER BY id DESC LIMIT 1",
                (occ, side_l, effect_l)).fetchone()
            if existing:
                intent = self._row_to_intent(existing)
                same_terms = (
                    intent.qty == qty_i and intent.order_type == type_l
                    and _px_eq(intent.limit_price, px))
                self._conn.commit()
                if same_terms:
                    logger.warning("[LEDGER] adopting open intent #%d (%s %s/%s x%d, state %s)",
                                   intent.id, occ, side_l, effect_l, qty_i, intent.state)
                    return intent, False
                raise IntentBlockedError(
                    f"open intent #{intent.id} ({intent.state}) already covers "
                    f"{occ} {side_l}/{effect_l} with different terms "
                    f"(x{intent.qty} {intent.order_type}"
                    f"{'@' + str(intent.limit_price) if intent.limit_price else ''}); "
                    f"cancel it (or resolve it) before re-intending")
            key = self.build_key(run_id, occ, side_l, effect_l, qty_i, type_l, px)
            now = _utcnow()
            try:
                cur = self._conn.execute(
                    "INSERT INTO intents (intent_key, ref_id, run_id, created_at, "
                    "updated_at, occ_symbol, side, effect, qty, order_type, "
                    "limit_price, account_last4, state) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, ref_id_for(key), run_id, now, now, occ, side_l,
                     effect_l, qty_i, type_l, px,
                     account_last4, "INTENDED"))
            except sqlite3.IntegrityError as e:
                # Same key retried inside one run (e.g. engine retry after a
                # non-crash error): adopt the row instead of duplicating.
                logger.warning("[SWALLOWED] duplicate intent_key %s adopted: %s", key, e)
                row = self._conn.execute(
                    "SELECT * FROM intents WHERE intent_key=?", (key,)).fetchone()
                self._conn.commit()
                return self._row_to_intent(row), False
            self._conn.commit()
            return self._get(cur.lastrowid), True

    # ------------------------------------------------------------ transitions
    def mark_placed(self, intent_id: int, broker_order_id: str) -> Intent:
        return self._set_state(intent_id, "PLACED",
                               broker_order_id=broker_order_id,
                               note=f"broker order {broker_order_id}")

    def mark_unconfirmed(self, intent_id: int, note: str = "") -> Intent:
        """The place call raised after the send: the order may have landed."""
        return self._set_state(
            intent_id, "PLACED_UNCONFIRMED",
            note=note or "place call raised after send; broker state unknown")

    def mark_sending(self, intent_id: int) -> Intent:
        """The transport send is about to happen.

        This is the crash-safety Rubicon and MUST be called immediately
        before the send (the adapter does this). A kill after this write
        leaves a SENDING row, which reconcile() promotes to
        PLACED_UNCONFIRMED and resolves against the broker -- it is never
        silently STALEd, so the next run cannot place a duplicate.
        """
        return self._set_state(
            intent_id, "SENDING",
            note="send attempted; broker state unknown until reconcile")

    def mark_terminal(self, intent_id: int, state: str, *,
                      fill_qty: float | None = None,
                      fill_avg_price: float | None = None,
                      note: str = "") -> Intent:
        if state not in ("FILLED", "CANCELLED", "REJECTED", "REJECTED_BY_REVIEW",
                         "DRY_RUN", "ABANDONED"):
            raise ValueError(f"not a terminal state: {state!r}")
        return self._set_state(intent_id, state, note=note,
                               fill_qty=fill_qty, fill_avg_price=fill_avg_price)

    def mark_by_broker_order(self, broker_order_id: str, state: str, *,
                             fill_qty: float | None = None,
                             fill_avg_price: float | None = None,
                             note: str = "") -> Intent | None:
        """Record a broker-observed outcome (poll/cancel hooks)."""
        if state not in ("FILLED", "CANCELLED", "REJECTED", "PLACED"):
            raise ValueError(f"not a broker-observable state: {state!r}")
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM intents WHERE broker_order_id=? "
                "ORDER BY id DESC LIMIT 1", (broker_order_id,)).fetchone()
            self._conn.commit()
        if not row:
            logger.warning("[LEDGER] broker order %s has no intent row; "
                           "not recording %s", broker_order_id, state)
            return None
        return self._set_state(row["id"], state, note=note,
                               fill_qty=fill_qty, fill_avg_price=fill_avg_price)

    # ------------------------------------------------------------ reconcile
    def _promote_interrupted_sends(self, run_id: str) -> int:
        """Crash recovery for the kill window between broker-ack and the
        ledger write (or mid-send with the response lost).

        A previous run's SENDING row may already be a live broker order, so
        it must NOT be STALEd: promote it to PLACED_UNCONFIRMED so the
        reconcile below resolves it against the broker (ref_id match ->
        adopted; no match -> NEEDS_REVIEW, never silently abandoned).
        Returns the number of rows promoted.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE intents SET state='PLACED_UNCONFIRMED', "
                "note='promoted by reconcile: the send may have completed "
                "before the crash; resolving against the broker', "
                "updated_at=? "
                "WHERE state='SENDING' AND run_id != ?",
                (_utcnow(), run_id))
            self._conn.commit()
            return cur.rowcount

    def reconcile(self, list_orders_fn, run_id=None) -> dict:
        """Resolve every PLACED_UNCONFIRMED intent against the broker.

        list_orders_fn: zero-arg callable returning a list of broker order
        dicts (id, state, legs[{side, position_effect}], quantity, and
        optionally ref_id / created_at).

        run_id: when given, previous runs' SENDING intents (killed between
        the send and the ledger write) are promoted to PLACED_UNCONFIRMED
        first, so they are resolved here instead of being STALEd by
        begin_intent() -- STALing them would allow a duplicate placement.

        Returns {"adopted": [...], "needs_review": [...]}.
        Raises IntentBlockedError (rows -> UNKNOWN) when the broker order
        list itself is unreadable: trading blind is refused.
        """
        if run_id is not None:
            promoted = self._promote_interrupted_sends(run_id)
            if promoted:
                logger.warning("[LEDGER] reconcile: promoted %d interrupted "
                               "send(s) to PLACED_UNCONFIRMED", promoted)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM intents WHERE state='PLACED_UNCONFIRMED' "
                "ORDER BY id").fetchall()
            pending = [self._row_to_intent(r) for r in rows]
            self._conn.commit()
        if not pending:
            return {"adopted": [], "needs_review": []}
        try:
            orders = list_orders_fn()
        except Exception as e:
            logger.error("[LEDGER] reconcile: broker order list unreadable: %r", e)
            for intent in pending:
                self._set_state(intent.id, "UNKNOWN",
                                note=f"reconcile failed: broker list unreadable ({e})")
            raise IntentBlockedError(
                "cannot reconcile unconfirmed intents: broker order list "
                f"unreadable ({e}). Refusing to trade blind.") from e
        report = {"adopted": [], "needs_review": []}
        for intent in pending:
            match = _match_order(intent, orders or [])
            if match == "AMBIGUOUS":
                self._set_state(intent.id, "NEEDS_REVIEW",
                                note="reconcile: multiple broker orders match; "
                                     "human must identify the real one")
                report["needs_review"].append(intent.id)
            elif match is None:
                self._set_state(intent.id, "NEEDS_REVIEW",
                                note="reconcile: no broker order matched; absence "
                                     "from the list is not proof of absence")
                report["needs_review"].append(intent.id)
            else:
                self._set_state(intent.id, "PLACED",
                                broker_order_id=str(match.get("id")),
                                note=f"reconcile: adopted broker order {match.get('id')}")
                report["adopted"].append(intent.id)
        return report

    # ------------------------------------------------------------ queries
    def get(self, intent_id: int) -> Intent | None:
        with self._lock:
            intent = self._get(intent_id)
            self._conn.commit()
            return intent

    def open_intents(self) -> list[Intent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM intents WHERE state IN "
                "('INTENDED','PLACED','PLACED_UNCONFIRMED','UNKNOWN') "
                "ORDER BY id").fetchall()
            self._conn.commit()
            return [self._row_to_intent(r) for r in rows]

    def list(self, state: str | None = None, limit: int = 50) -> list[Intent]:
        with self._lock:
            if state:
                rows = self._conn.execute(
                    "SELECT * FROM intents WHERE state=? ORDER BY id DESC LIMIT ?",
                    (state.upper(), limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM intents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            self._conn.commit()
            return [self._row_to_intent(r) for r in rows]

    def resolve(self, intent_id: int, state: str, *,
                broker_order_id: str | None = None, note: str = "") -> Intent:
        """Human resolution of a NEEDS_REVIEW / UNKNOWN intent."""
        if state not in ("PLACED", "ABANDONED"):
            raise ValueError("resolve target must be PLACED or ABANDONED")
        intent = self.get(intent_id)
        if intent is None:
            raise KeyError(f"no intent #{intent_id}")
        if intent.state not in ("NEEDS_REVIEW", "UNKNOWN"):
            raise ValueError(f"intent #{intent_id} is {intent.state}, "
                             "only NEEDS_REVIEW/UNKNOWN can be resolved")
        return self._set_state(intent_id, state, note=note or "human resolved",
                               broker_order_id=broker_order_id)


def _match_order(intent: Intent, orders: list) -> dict | None | str:
    """Match an unconfirmed intent to a broker order.

    Returns the order dict, None (no match), or "AMBIGUOUS" (>1 match).
    ref_id is authoritative when present. The side+qty+time fallback only
    matches orders with NO ref_id: an order stamped with a different
    intent's ref_id belongs to that intent and is never adopted here.
    Naive (offset-less) broker timestamps are treated as UTC rather than
    crashing the reconcile.
    """
    by_ref = [o for o in orders
              if str(o.get("ref_id") or "") == intent.ref_id]
    if len(by_ref) == 1:
        return by_ref[0]
    if len(by_ref) > 1:
        return "AMBIGUOUS"
    try:
        created = datetime.fromisoformat(intent.created_at)
    except ValueError:
        logger.warning("[SWALLOWED] intent #%d has unparseable created_at %r; "
                       "skipping time-window match", intent.id, intent.created_at)
        return None
    cands = []
    for o in orders:
        # ref_id is authoritative: an order stamped with a DIFFERENT
        # intent's ref_id belongs to that intent and must never be adopted
        # by the weak side/qty/time fallback below. (A matching ref_id was
        # already consumed by the primary loop above.)
        if str(o.get("ref_id") or "") and str(o.get("ref_id")) != intent.ref_id:
            continue
        legs = o.get("legs") or []
        leg = legs[0] if legs else {}
        o_side = str(leg.get("side") or "").lower()
        o_effect = str(leg.get("position_effect") or "").lower()
        try:
            o_qty = int(float(o.get("quantity") or 0))
        except (TypeError, ValueError):
            logger.debug("[SWALLOWED] broker order %s unparseable quantity %r",
                         o.get("id"), o.get("quantity"))
            continue
        if o_side != intent.side or o_effect != intent.effect or o_qty != intent.qty:
            continue
        ts = o.get("created_at") or o.get("created_time")
        if not ts:
            continue
        try:
            o_created = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            logger.debug("[SWALLOWED] broker order %s unparseable created_at %r",
                         o.get("id"), ts)
            continue
        if o_created.tzinfo is None:
            # The broker omitted the UTC offset. Normalize to UTC (the
            # broker's API emits UTC) rather than letting a naive/aware
            # subtraction raise TypeError and kill the whole reconcile.
            o_created = o_created.replace(tzinfo=timezone.utc)
        if abs((o_created - created).total_seconds()) <= RECONCILE_WINDOW_S:
            cands.append(o)
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        return "AMBIGUOUS"
    return None


# --------------------------------------------------------------------- CLI
def _default_db() -> Path:
    here = Path(__file__).resolve()
    # core/order_intent_ledger.py -> repo root -> state/order_intents.db
    return here.parent.parent / "state" / "order_intents.db"


def _fmt(i: Intent) -> str:
    px = f"@{i.limit_price}" if i.limit_price is not None else "@mkt"
    bo = i.broker_order_id or "-"
    return (f"#{i.id} [{i.state}] {i.occ_symbol} {i.side}/{i.effect} "
            f"x{i.qty} {i.order_type}{px} acct=****{i.account_last4} "
            f"broker={bo} run={i.run_id[:8]} {i.created_at[:19]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Order-intent ledger ops")
    ap.add_argument("--db", default=str(_default_db()))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="recent intents")
    p.add_argument("--state", default=None)
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("show", help="one intent")
    p.add_argument("id", type=int)
    p = sub.add_parser("resolve", help="human-resolve NEEDS_REVIEW/UNKNOWN")
    p.add_argument("id", type=int)
    p.add_argument("state", choices=["PLACED", "ABANDONED"])
    p.add_argument("--broker-order-id", default=None)
    p.add_argument("--note", default="")
    a = ap.parse_args(argv)
    ledger = OrderIntentLedger(a.db)
    if a.cmd == "list":
        for i in ledger.list(state=a.state, limit=a.limit):
            print(_fmt(i))
    elif a.cmd == "show":
        i = ledger.get(a.id)
        print(_fmt(i) if i else f"no intent #{a.id}")
    elif a.cmd == "resolve":
        i = ledger.resolve(a.id, a.state,
                           broker_order_id=a.broker_order_id, note=a.note)
        print(_fmt(i))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
