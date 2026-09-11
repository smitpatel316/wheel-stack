"""Tests for core/order_intent_ledger.py + its wiring in robinhood_broker.

All broker transport is stubbed; nothing here touches the network.
"""
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.order_intent_ledger import (  # noqa: E402
    IntentBlockedError,
    OrderIntentLedger,
    ref_id_for,
)


@pytest.fixture()
def ledger(tmp_path):
    return OrderIntentLedger(tmp_path / "intents.db")


def _begin(ledger, run_id="run1", occ="F260926P00012000", side="sell",
           effect="open", qty=1, order_type="market", limit_price=None):
    return ledger.begin_intent(run_id=run_id, occ_symbol=occ, side=side,
                               effect=effect, qty=qty, order_type=order_type,
                               limit_price=limit_price, account_last4="5913")


# ---------------------------------------------------------------- keys
def test_ref_id_namespace_pins_rh_order_client():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                            / "robinhood-mcp"))
    from rh_order_client import UUID5_NS  # noqa
    from core.order_intent_ledger import UUID5_NAMESPACE
    assert UUID5_NAMESPACE == UUID5_NS


def test_key_enriched_with_price_and_run():
    l1 = OrderIntentLedger.__new__(OrderIntentLedger)
    k_mkt = l1.build_key("r1", "F260926P00012000", "sell", "open", 1, "market", None)
    k_lim = l1.build_key("r1", "F260926P00012000", "sell", "open", 1, "limit", 1.66)
    k_r2 = l1.build_key("r2", "F260926P00012000", "sell", "open", 1, "market", None)
    assert len({k_mkt, k_lim, k_r2}) == 3  # no over-dedupe: price and run differ
    assert ref_id_for(k_mkt) == str(uuid.uuid5(uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), k_mkt))


def test_same_key_is_idempotent(ledger):
    i1, created1 = _begin(ledger)
    i2, created2 = _begin(ledger)
    assert created1 is True and created2 is False
    assert i1.id == i2.id
    assert ledger.list()[0].state == "INTENDED"


def test_different_terms_blocked_while_open(ledger):
    # One open intent per contract+side+effect: a limit order working at 1.66
    # must be cancelled before a market order for the same trade is intended.
    lim, _ = _begin(ledger, order_type="limit", limit_price=1.66)
    ledger.mark_placed(lim.id, broker_order_id="ord-1")
    with pytest.raises(IntentBlockedError, match="different terms"):
        _begin(ledger, order_type="market")
    # After the working order is cancelled, the re-intent proceeds.
    ledger.mark_terminal(lim.id, "CANCELLED", note="limit unfilled")
    mkt, created = _begin(ledger, order_type="market")
    assert created is True and mkt.id != lim.id


def test_same_terms_adopted_across_runs(ledger):
    old, _ = _begin(ledger, run_id="run-old", order_type="limit", limit_price=1.66)
    ledger.mark_placed(old.id, broker_order_id="ord-1")
    adopted, created = _begin(ledger, run_id="run-new",
                              order_type="limit", limit_price=1.66)
    assert created is False and adopted.id == old.id


# ---------------------------------------------------------------- lifecycle
def test_place_and_fill_lifecycle(ledger):
    intent, _ = _begin(ledger)
    ledger.mark_placed(intent.id, broker_order_id="ord-1")
    assert ledger.get(intent.id).state == "PLACED"
    ledger.mark_by_broker_order("ord-1", "FILLED", fill_qty=1, fill_avg_price=1.56)
    done = ledger.get(intent.id)
    assert done.state == "FILLED" and done.fill_qty == 1 and done.fill_avg_price == 1.56


def test_review_blocked_is_terminal(ledger):
    intent, _ = _begin(ledger)
    ledger.mark_terminal(intent.id, "REJECTED_BY_REVIEW", note="order_checks")
    assert ledger.get(intent.id).state == "REJECTED_BY_REVIEW"
    with pytest.raises(ValueError):
        ledger.mark_terminal(intent.id, "PLACED")  # not terminal


def test_dry_run_recorded(ledger):
    intent, _ = _begin(ledger)
    ledger.mark_terminal(intent.id, "DRY_RUN", note="review_clean=True (never placed)")
    assert ledger.get(intent.id).state == "DRY_RUN"
    assert ledger.get(intent.id).broker_order_id is None


def test_stale_previous_run_intended(ledger):
    old, _ = _begin(ledger, run_id="run-old")
    new, created = _begin(ledger, run_id="run-new", occ="G260926P00010000")
    assert created is True
    assert ledger.get(old.id).state == "STALE"
    assert ledger.get(new.id).state == "INTENDED"


def test_adopt_open_intent_across_runs(ledger):
    old, _ = _begin(ledger, run_id="run-old")
    ledger.mark_placed(old.id, broker_order_id="ord-9")
    adopted, created = _begin(ledger, run_id="run-new")  # same contract
    assert created is False and adopted.id == old.id


# ---------------------------------------------------------------- reconcile
def _broker_order(ref_id=None, oid="ord-1", side="sell", effect="open",
                  qty=1, minutes_ago=2):
    created = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {"id": oid, "state": "confirmed", "ref_id": ref_id,
            "quantity": str(qty), "created_at": created,
            "legs": [{"side": side, "position_effect": effect}]}


def test_reconcile_adopts_by_ref_id(ledger):
    intent, _ = _begin(ledger)
    ledger.mark_unconfirmed(intent.id)
    orders = [_broker_order(ref_id=intent.ref_id, oid="ord-live")]
    report = ledger.reconcile(lambda: orders)
    assert report == {"adopted": [intent.id], "needs_review": []}
    done = ledger.get(intent.id)
    assert done.state == "PLACED" and done.broker_order_id == "ord-live"


def test_reconcile_fallback_side_qty_time(ledger):
    intent, _ = _begin(ledger)
    ledger.mark_unconfirmed(intent.id)
    orders = [_broker_order(ref_id="something-else", oid="ord-x")]  # no ref_id field match
    report = ledger.reconcile(lambda: orders)
    assert report["adopted"] == [intent.id]
    assert ledger.get(intent.id).broker_order_id == "ord-x"


def test_reconcile_no_match_needs_review_and_blocks(ledger):
    intent, _ = _begin(ledger)
    ledger.mark_unconfirmed(intent.id)
    report = ledger.reconcile(lambda: [])  # broker shows nothing
    assert report["needs_review"] == [intent.id]
    assert ledger.get(intent.id).state == "NEEDS_REVIEW"
    # Absence from the list is not proof of absence: re-intending is refused.
    with pytest.raises(IntentBlockedError):
        _begin(ledger, run_id="run-new")
    # Human resolution unblocks.
    ledger.resolve(intent.id, "ABANDONED", note="verified with broker by hand")
    assert ledger.get(intent.id).state == "ABANDONED"
    fresh, created = _begin(ledger, run_id="run-new")
    assert created is True


def test_reconcile_ambiguous_needs_review(ledger):
    intent, _ = _begin(ledger)
    ledger.mark_unconfirmed(intent.id)
    orders = [_broker_order(ref_id="nope", oid="ord-a"),
              _broker_order(ref_id="nope", oid="ord-b")]
    report = ledger.reconcile(lambda: orders)
    assert report["needs_review"] == [intent.id]


def test_reconcile_broker_unreadable_fails_closed(ledger):
    intent, _ = _begin(ledger)
    ledger.mark_unconfirmed(intent.id)

    def _boom():
        raise RuntimeError("gateway 502")

    with pytest.raises(IntentBlockedError, match="unreadable"):
        ledger.reconcile(_boom)
    assert ledger.get(intent.id).state == "UNKNOWN"
    with pytest.raises(IntentBlockedError):
        _begin(ledger, run_id="run-new")


def test_reconcile_nothing_pending_no_list_call(ledger):
    calls = []
    report = ledger.reconcile(lambda: calls.append(1) or [])
    assert report == {"adopted": [], "needs_review": []}
    assert calls == []


# ---------------------------------------------------------------- adapter wiring
def _adapter(monkeypatch, tmp_path, dry_run=False, **kw):
    import core.robinhood_broker as rb
    monkeypatch.setenv("RH_LIVE_ORDERS", "true")
    monkeypatch.delenv("RH_DRY_RUN", raising=False)
    ledger = OrderIntentLedger(tmp_path / "intents.db")
    a = rb.RobinhoodBrokerClient(data_client=None, live=True,
                                 dry_run=dry_run, ledger=ledger)
    # Stub the network edge of the adapter.
    monkeypatch.setattr(rb, "get_agentic_account",
                        lambda *a_, **k: {"account_number": "000000005913"})
    monkeypatch.setattr(rb.RobinhoodBrokerClient, "_option_id",
                        lambda self, occ: "opt-uuid-1")
    return a, ledger, rb


def test_adapter_dry_run_records_intent_never_places(monkeypatch, tmp_path):
    a, ledger, rb = _adapter(monkeypatch, tmp_path, dry_run=True)
    placed = []
    monkeypatch.setattr(rb, "place_option_order",
                        lambda *a_, **k: placed.append(1) or {"id": "x"})
    monkeypatch.setattr(rb, "dry_run_review",
                        lambda *a_, **k: {"review_clean": True})
    view = a._place("F260926P00012000", "sell", "open", 1)
    assert placed == [] and view.id.startswith("dry-run-")
    intents = ledger.list()
    assert len(intents) == 1 and intents[0].state == "DRY_RUN"
    assert intents[0].broker_order_id is None


def test_adapter_place_marks_placed_and_unconfirmed_on_crash(monkeypatch, tmp_path):
    a, ledger, rb = _adapter(monkeypatch, tmp_path)
    monkeypatch.setattr(rb, "place_option_order",
                        lambda *a_, **k: {"id": "ord-42", "state": "confirmed"})
    view = a._place("F260926P00012000", "sell", "open", 1, limit_price=1.66)
    assert view.id == "ord-42"
    intent = ledger.list()[0]
    assert intent.state == "PLACED" and intent.broker_order_id == "ord-42"
    # The logical key handed to the transport is the enriched intent key.
    assert intent.intent_key.endswith(":limit:1.6600")

    a2, ledger2, rb2 = _adapter(monkeypatch, tmp_path)  # new run, same DB
    def _boom(*a_, **k):
        raise RuntimeError("transport died after send")
    monkeypatch.setattr(rb2, "place_option_order", _boom)
    # Same contract already PLACED -> adopted, transport never called again.
    monkeypatch.setattr(rb2, "get_option_order",
                        lambda oid: {"id": oid, "state": "confirmed", "legs": []})
    view2 = a2._place("F260926P00012000", "sell", "open", 1, limit_price=1.66)
    assert view2.id == "ord-42"

    # A *different* price while the first is still working is refused --
    # the engine must cancel the working order before re-intending.
    with pytest.raises(IntentBlockedError, match="different terms"):
        a2._place("F260926P00012000", "sell", "open", 1, limit_price=1.70)

    # Crash on a fresh contract -> PLACED_UNCONFIRMED.
    def _boom(*a_, **k):
        raise RuntimeError("transport died after send")
    monkeypatch.setattr(rb2, "place_option_order", _boom)
    with pytest.raises(RuntimeError):
        a2._place("G260926P00010000", "sell", "open", 1)
    rows = ledger2.list(state="PLACED_UNCONFIRMED")
    assert len(rows) == 1 and rows[0].occ_symbol == "G260926P00010000"


def test_adapter_review_blocked_terminal(monkeypatch, tmp_path):
    a, ledger, rb = _adapter(monkeypatch, tmp_path)
    from rh_order_client import RHReviewBlockedError
    def _blocked(*a_, **k):
        raise RHReviewBlockedError("order_checks")
    monkeypatch.setattr(rb, "place_option_order", _blocked)
    with pytest.raises(RHReviewBlockedError):
        a._place("F260926P00012000", "sell", "open", 1)
    assert ledger.list()[0].state == "REJECTED_BY_REVIEW"


def test_adapter_wait_for_fill_records_outcome(monkeypatch, tmp_path):
    a, ledger, rb = _adapter(monkeypatch, tmp_path)
    monkeypatch.setattr(rb, "place_option_order",
                        lambda *a_, **k: {"id": "ord-7", "state": "confirmed"})
    a._place("F260926P00012000", "sell", "open", 1)
    monkeypatch.setattr(rb, "wait_for_fill",
                        lambda oid, timeout_s=30.0: {
                            "state": "filled", "filled_qty": 1,
                            "avg_fill_price": 1.56, "order": {}})
    out = a.wait_for_fill("ord-7")
    assert out["state"] == "filled"
    done = ledger.list()[0]
    assert done.state == "FILLED" and done.fill_avg_price == 1.56


def test_adapter_reconcile_runs_once_before_first_place(monkeypatch, tmp_path):
    a, ledger, rb = _adapter(monkeypatch, tmp_path)
    lists = []
    monkeypatch.setattr(rb, "_call_tool",
                        lambda *a_, **k: lists.append(1) or {"orders": []})
    intent, _ = _begin(ledger)
    ledger.mark_unconfirmed(intent.id)
    monkeypatch.setattr(rb, "place_option_order",
                        lambda *a_, **k: {"id": "ord-1", "state": "confirmed"})
    # Reconcile finds nothing -> NEEDS_REVIEW -> begin_intent raises.
    with pytest.raises(IntentBlockedError):
        a._place("F260926P00012000", "sell", "open", 1)
    assert lists == [1]  # exactly one broker list call
    assert a._reconciled is True


# ---------------------------------------------------------------- SENDING Rubicon
def test_sending_promoted_and_adopted_on_reconcile(ledger):
    """Kill between broker-ack and mark_placed: the SENDING row must be
    adopted by ref_id on the next run, never STALEd into a duplicate."""
    intent, _ = _begin(ledger, run_id="run1", order_type="limit",
                       limit_price=1.55)
    ledger.mark_sending(intent.id)
    # ... process killed here; the broker HAS the order ...
    orders = [{"id": "o-live", "ref_id": intent.ref_id, "state": "confirmed",
               "quantity": "1", "created_at": intent.created_at,
               "legs": [{"side": "sell", "position_effect": "open"}]}]
    report = ledger.reconcile(lambda: orders, run_id="run2")
    assert report == {"adopted": [intent.id], "needs_review": []}
    done = ledger.get(intent.id)
    assert done.state == "PLACED" and done.broker_order_id == "o-live"


def test_sending_no_match_needs_review_and_blocks(ledger):
    """SENDING with no broker match: fail closed, never silently STALEd."""
    intent, _ = _begin(ledger, run_id="run1")
    ledger.mark_sending(intent.id)
    report = ledger.reconcile(lambda: [], run_id="run2")
    assert report["needs_review"] == [intent.id]
    assert ledger.get(intent.id).state == "NEEDS_REVIEW"
    with pytest.raises(IntentBlockedError):
        _begin(ledger, run_id="run2")
    ledger.resolve(intent.id, "ABANDONED", note="verified never sent")
    fresh, created = _begin(ledger, run_id="run2")
    assert created is True


def test_begin_intent_refuses_unreconciled_sending(ledger):
    """begin_intent without a prior reconcile must not adopt/STALE a
    previous run's SENDING row."""
    intent, _ = _begin(ledger, run_id="run1")
    ledger.mark_sending(intent.id)
    with pytest.raises(IntentBlockedError, match="reconcile"):
        _begin(ledger, run_id="run2")


def test_previous_run_intended_without_sending_still_staled(ledger):
    """INTENDED rows whose send was never attempted (SENDING never written)
    are provably never-sent: STALing stays sound."""
    old, _ = _begin(ledger, run_id="run-old")
    new, created = _begin(ledger, run_id="run-new", occ="G260926P00010000")
    assert created is True
    assert ledger.get(old.id).state == "STALE"
    assert ledger.get(new.id).state == "INTENDED"


def test_adapter_marks_sending_before_transport(monkeypatch, tmp_path):
    """The adapter writes the SENDING Rubicon before the transport send, so
    a kill inside place_option_order is recoverable via reconcile."""
    a, ledger, rb = _adapter(monkeypatch, tmp_path)
    seen = {}

    def _spy(*a_, **k):
        seen["state"] = ledger.list()[0].state
        return {"id": "ord-1", "state": "confirmed"}

    monkeypatch.setattr(rb, "place_option_order", _spy)
    a._place("F260926P00012000", "sell", "open", 1)
    assert seen["state"] == "SENDING"
    assert ledger.list()[0].state == "PLACED"


def test_reconcile_without_run_id_skips_promotion(ledger):
    """Backward compatible: reconcile() with no run_id behaves as before."""
    intent, _ = _begin(ledger, run_id="run1")
    ledger.mark_sending(intent.id)
    report = ledger.reconcile(lambda: [])
    assert report == {"adopted": [], "needs_review": []}
    # SENDING untouched without a run_id to compare against.
    assert ledger.get(intent.id).state == "SENDING"
