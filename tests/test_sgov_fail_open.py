"""SGOV sweep fail-closed regression tests (P6 audit, 2026-09-09).

sync_sgov_float() must ABORT (raise SGOVStateUnreadable) when it cannot read
the broker state its safety guards depend on -- never assume zero:

1. get_orders() raising must not be treated as "zero pending orders". The
   duplicate-buy guard in decide_float_order ("open SGOV BUY suppresses any
   new buy") is blind without the order list, so the sweep could double-buy.
2. An SGOV position with unparseable qty must not be treated as qty 0. The
   sweep would believe it holds nothing and over-buy.

Both are fail-open on the money path; the fix fails closed. A mocked broker
is used throughout -- no live broker calls.
"""
import logging
from types import SimpleNamespace

import contextlib

import pytest

import core.sgov_float as _sgov_float_mod
from core.sgov_float import sync_sgov_float


# The fail-closed exception is introduced by the fix. Pre-fix it does not
# exist; the helper below then forces the raises-check to fail so the tests
# still run and demonstrate the fail-open ordering instead of erroring at
# import time.
_SGOVStateUnreadable = getattr(_sgov_float_mod, "SGOVStateUnreadable", None)


@contextlib.contextmanager
def _expect_sgov_abort(match):
    if _SGOVStateUnreadable is None:
        with pytest.raises(RuntimeError, match="no-such-failure"):
            yield
    else:
        with pytest.raises(_SGOVStateUnreadable, match=match):
            yield


PRICE = 100.50


class _Acct:
    def __init__(self, cash=0.0, equity=0.0, buying_power=0.0,
                 options_buying_power=0.0):
        self.cash = cash
        self.equity = equity
        self.buying_power = buying_power
        self.options_buying_power = options_buying_power


class _ExplodingTradeClient:
    """get_orders always raises: the broker's order list is unreadable."""

    def get_orders(self, filter=None):
        raise RuntimeError("broker order list unavailable")


class _TradeClient:
    def __init__(self, orders=()):
        self._orders = list(orders)

    def get_orders(self, filter=None):
        return list(self._orders)


class _Client:
    """Minimal broker fake: no network, orders go through the injected fn."""

    def __init__(self, *, equity, cash=0.0, stock_bp=0.0, opt_bp=0.0,
                 sgov_qty="0", trade_client=None):
        self.account = _Acct(cash=cash, equity=equity, buying_power=stock_bp,
                             options_buying_power=opt_bp)
        self.positions = [SimpleNamespace(symbol="SGOV", qty=sgov_qty,
                                          current_price=str(PRICE))]
        self.trade_client = (trade_client if trade_client is not None
                             else _TradeClient())

    def get_positions(self):
        return list(self.positions)

    def get_account(self):
        return self.account

    def get_stock_latest_trade(self, symbols):
        return {"SGOV": SimpleNamespace(price=PRICE)}


class _CountingOrderFn:
    def __init__(self):
        self.calls = []

    def __call__(self, client, side, qty, logger_obj=None):
        self.calls.append((side, qty))


@pytest.fixture(autouse=True)
def _isolated_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("WHEEL_FUNDING_QUEUE", str(tmp_path / "funding_queue.json"))


def _would_buy_client(**kwargs):
    """equity $60k, cap $500 -> float target $59,500, nothing held: without
    the fail-closed guard the sweep buys 592 shares (floor(59500/100.50))."""
    return _Client(equity=60_000, cash=60_000, stock_bp=1_000_000, **kwargs)


def test_order_list_failure_aborts_instead_of_assuming_zero_pending():
    """get_orders() raising must abort the sweep, not read as zero pending
    orders (which blinds the duplicate-buy guard)."""
    c = _would_buy_client(trade_client=_ExplodingTradeClient())
    order_fn = _CountingOrderFn()
    with _expect_sgov_abort("pending orders"):
        sync_sgov_float(c, logging.getLogger("t.failopen.orders"),
                        equity=60_000, risk_cap=500,
                        enabled=True, order_fn=order_fn)
    assert order_fn.calls == [], order_fn.calls


@pytest.mark.parametrize("bad_qty", ["N/A", "", None, "ten"])
def test_unparseable_sgov_qty_aborts_instead_of_assuming_zero(bad_qty):
    """A malformed SGOV position qty must abort the sweep, not read as zero
    holdings (which would over-buy)."""
    c = _would_buy_client(sgov_qty=bad_qty)
    order_fn = _CountingOrderFn()
    with _expect_sgov_abort("not parseable"):
        sync_sgov_float(c, logging.getLogger("t.failopen.qty"),
                        equity=60_000, risk_cap=500,
                        enabled=True, order_fn=order_fn)
    assert order_fn.calls == [], order_fn.calls


def test_unparseable_sgov_price_still_falls_back():
    """Only the qty parse is fail-closed; an unparseable position *price*
    keeps the existing lenient fallback (latest-trade price), so the sweep
    still works: held 100 sh @ $100.50 = $10,050 vs target $59,500 ->
    buy 592-100 = 492."""
    c = _would_buy_client(sgov_qty="100")
    c.positions[0].current_price = "bogus"
    order_fn = _CountingOrderFn()
    sync_sgov_float(c, logging.getLogger("t.failopen.price"),
                    equity=60_000, risk_cap=500,
                    enabled=True, order_fn=order_fn)
    assert order_fn.calls == [("buy", 492)], order_fn.calls
