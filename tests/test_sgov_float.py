"""SGOV float model unit tests (v2.8, 2026-08-28 per Smit).

Target: structural float only — SGOV market value should track
max(0, account_equity - effective_risk_cap). Cash inside the cap is never
swept (deployed collateral AND sub-contract slack stay liquid). A $2,000
rebalance band (SGOV_REBALANCE_BAND, env-overridable) suppresses churn.
SGOV_ENABLED is the complete kill switch: no orders, no [SGOV FLOAT]
action-intent logs while disabled (clean-week guarantee).
"""
import logging
from types import SimpleNamespace

import pytest

from core.sgov_float import compute_float_target, decide_float_order, sync_sgov_float


PRICE = 100.50


def test_target_floor_at_zero():
    """Equity below the cap -> float is 0, never negative."""
    assert compute_float_target(50_000, 80_000) == 0.0
    assert compute_float_target(100_000, 100_000) == 0.0
    assert compute_float_target(120_000, 100_000) == 20_000.0


def test_hold_within_tolerance_band():
    """Drift below SGOV_REBALANCE_BAND must not trade (no per-run churn)."""
    target = 6_000.0
    held_qty = 50  # 50 * 100.50 = 5,025 -> drift $975 < $2,000 band
    action, qty, reason = decide_float_order(target, held_qty, PRICE)
    assert (action, qty) == ("hold", 0)
    assert "band" in reason


def test_buy_when_float_above_band():
    """Float target above holdings by >= band -> buy the share floor."""
    target = 60_000.0
    held_qty = 500  # $50,250 -> drift $9,750
    # desired = floor(60000/100.50) = 597 -> buy 597 - 500 = 97
    action, qty, _ = decide_float_order(target, held_qty, PRICE)
    assert (action, qty) == ("buy", 97)


class _Acct:
    def __init__(self, cash=0.0, equity=0.0, buying_power=0.0, options_buying_power=0.0):
        self.cash = cash
        self.equity = equity
        self.buying_power = buying_power
        self.options_buying_power = options_buying_power


class _TradeClient:
    def __init__(self, orders=()):
        self._orders = list(orders)

    def get_orders(self, filter=None):
        return list(self._orders)


class _Client:
    """Minimal broker fake: no network, records market stock orders."""

    def __init__(self, *, equity, cash=0.0, stock_bp=0.0, opt_bp=0.0,
                 sgov_qty=0, open_orders=()):
        self.account = _Acct(cash=cash, equity=equity, buying_power=stock_bp,
                             options_buying_power=opt_bp)
        self.positions = []
        if sgov_qty:
            self.positions.append(SimpleNamespace(symbol="SGOV", qty=str(sgov_qty),
                                                  current_price=str(PRICE)))
        self.trade_client = _TradeClient(open_orders)
        self.stock_orders = []  # ("buy"/"sell", symbol, qty)

    def get_positions(self):
        return list(self.positions)

    def get_account(self):
        return self.account

    def get_stock_latest_trade(self, symbols):
        return {"SGOV": SimpleNamespace(price=PRICE)}

    def market_buy(self, symbol, qty=1):
        self.stock_orders.append(("buy", symbol, qty))

    def market_sell_qty(self, symbol, qty=1):
        self.stock_orders.append(("sell", symbol, qty))


@pytest.fixture(autouse=True)
def _isolated_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("WHEEL_FUNDING_QUEUE", str(tmp_path / "funding_queue.json"))


def test_sell_capped_by_available_after_pending():
    """Never sell more SGOV than available after pending sells (ledger +
    open orders). Held 500, 100 sold pending -> only 400 touchable; a target
    of $20,000 (199 sh) may sell exactly 400-199=201, not 500-199=301."""
    open_order = SimpleNamespace(symbol="SGOV", qty=100, side="sell", status="new")
    held_500 = _Client(equity=50_250 + 100, stock_bp=1_000_000, sgov_qty=500,
                       open_orders=[open_order])
    calls = []
    action, qty, _ = decide_float_order(
        20_000.0, 500, PRICE, pending_sell_qty=100)
    assert (action, qty) == ("sell", 201)
    # Same through the full sync path (open order feeds the pending count):
    log = logging.getLogger("t.sellcap")
    sync_sgov_float(held_500, log, equity=50_350, risk_cap=30_350,
                    enabled=True,
                    order_fn=lambda c, side, q, logger_obj=None: calls.append((side, q)))
    assert calls == [("sell", 201)], calls


def test_pending_sell_suppresses_buy_back_churn():
    """Target moves UP while a sell is pending -> hold (no buy-back churn)."""
    action, qty, reason = decide_float_order(
        100_000.0, 500, PRICE, pending_sell_qty=50)
    assert (action, qty) == ("hold", 0)
    assert "pending" in reason


def test_low_buying_power_never_forces_a_sell():
    """$0 stock BP caps PURCHASES to 0 shares; holding SGOV consumes no BP,
    so low BP must hold, never 'force' a sell (2026-08-21 lesson)."""
    action, qty, _ = decide_float_order(
        66_908.0, 616, PRICE, buy_capacity_usd=0.0)
    assert (action, qty) == ("hold", 0)


def test_buy_capped_by_stock_bp_buffer():
    """Buy capacity = floor((stock_bp - $1,000 buffer)/price) guards the
    $1k buffer the engine keeps for stock assignments."""
    # target far above: unconstrained diff would be 497
    action, qty, _ = decide_float_order(
        100_000.0, 0, PRICE, buy_capacity_usd=10_000.0)
    assert (action, qty) == ("buy", 99)  # floor(10000/100.50)


class _CountingOrderFn:
    def __init__(self):
        self.calls = []

    def __call__(self, client, side, qty, logger_obj=None):
        self.calls.append((side, qty))


def test_disabled_places_no_orders():
    """Kill switch: enabled=False -> no order helper call, no broker order.
    (Scenario would buy 590 shares if enabled.)"""
    c = _Client(equity=60_000, cash=60_000, stock_bp=1_000_000, sgov_qty=0)
    order_fn = _CountingOrderFn()
    sync_sgov_float(c, logging.getLogger("t.disabled"), equity=60_000,
                    risk_cap=500, enabled=False, order_fn=order_fn)
    assert order_fn.calls == []
    assert c.stock_orders == []


def test_disabled_emits_no_action_logs(caplog):
    """Kill switch: disabled runs must not log a [SGOV FLOAT] line at all —
    no 'buy/sell' action intent may appear while SGOV is off."""
    c = _Client(equity=60_000, cash=60_000, stock_bp=1_000_000, sgov_qty=100)
    with caplog.at_level(logging.DEBUG):
        sync_sgov_float(c, logging.getLogger("t.disabledlogs"), equity=60_000,
                        risk_cap=500, enabled=False, order_fn=_CountingOrderFn())
    sgov_lines = [r.getMessage() for r in caplog.records
                  if "[SGOV" in r.getMessage()]
    assert sgov_lines == [], sgov_lines


def test_enabled_logs_float_line_and_orders(caplog):
    """Enabled: exactly one [SGOV FLOAT] status line + the dashboard-compat
    [SGOV] target line, and the order goes through the injected helper."""
    c = _Client(equity=60_000, cash=60_000, stock_bp=1_000_000, sgov_qty=0)
    order_fn = _CountingOrderFn()
    with caplog.at_level(logging.INFO):
        sync_sgov_float(c, logging.getLogger("t.enabled"), equity=60_000,
                        risk_cap=500, enabled=True, order_fn=order_fn)
    float_lines = [r.getMessage() for r in caplog.records
                   if r.getMessage().startswith("[SGOV FLOAT]")]
    assert len(float_lines) == 1
    # target 59,500 -> floor(59500/100.50)=592 shares, uncapped by BP.
    assert "-> buy 592" in float_lines[0], float_lines[0]
    assert order_fn.calls == [("buy", 592)]


def test_run_strategy_kill_switch_belt(monkeypatch, caplog):
    """The run_strategy wrapper also refuses to act (or log) when its
    SGOV_ENABLED flag is False, so no call-site can bypass the kill switch."""
    import scripts.run_strategy as rs
    monkeypatch.setattr(rs, "SGOV_ENABLED", False)
    c = _Client(equity=60_000, cash=60_000, stock_bp=1_000_000, sgov_qty=100)
    with caplog.at_level(logging.DEBUG):
        rs.sync_sgov_real(c, logging.getLogger("t.belt"), risk_cap=500)
    assert c.stock_orders == []
    assert [r for r in caplog.records if "[SGOV" in r.getMessage()] == []
