"""Offline fakes for wheel-stack stress tests.

FakeBrokerClient mirrors the interface of core/broker_client.py:
  get_account, get_positions, get_options_contracts, get_option_snapshot,
  get_stock_latest_trade, market_buy, market_sell_qty, market_sell,
  limit_sell, get_order, cancel_order, liquidate_all_positions, trade_client

No network. Everything is settable so tests can drive any account state.
"""
from __future__ import annotations

import itertools
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional

_order_ids = itertools.count(1)


def _oid():
    return f"fake-order-{next(_order_ids)}"


class FakeAccount:
    def __init__(self, cash=575.0, equity=100_000.0, buying_power=133_000.0,
                 options_buying_power=14_000.0, multiplier="4"):
        self.cash = cash
        self.equity = equity
        self.buying_power = buying_power            # stock BP (margin-inflated)
        self.options_buying_power = options_buying_power
        self.multiplier = multiplier


class FakePosition:
    """asset_class: 'us_equity' or 'us_option' (strings OK — state_manager
    compares against alpaca AssetClass enum; tests that need it get the real
    enum via make_position())."""

    def __init__(self, symbol, qty, avg_entry_price=100.0, current_price=100.0,
                 asset_class="us_equity"):
        self.symbol = symbol
        self.qty = str(qty)
        self.avg_entry_price = str(avg_entry_price)
        self.current_price = str(current_price)
        self.asset_class = asset_class


class FakeOrder:
    def __init__(self, symbol, qty, side, order_type="market", limit_price=None,
                 status="accepted", filled_avg_price=None):
        self.id = _oid()
        self.symbol = symbol
        self.qty = qty
        self.side = side
        self.type = order_type
        self.limit_price = limit_price
        self.status = status
        self.filled_avg_price = filled_avg_price


class FakeOptionContractRaw:
    """Mimics an Alpaca OptionContract (input to Contract.from_contract_snapshot)."""

    def __init__(self, symbol, underlying_symbol, strike_price, expiration_date,
                 open_interest=500):
        self.symbol = symbol
        self.underlying_symbol = underlying_symbol
        self.strike_price = strike_price
        self.expiration_date = expiration_date
        self.open_interest = open_interest


def make_occ(underlying: str, exp: date, pc: str, strike: float) -> str:
    return f"{underlying}{exp.strftime('%y%m%d')}{pc}{int(round(strike * 1000)):08d}"


def make_put(underlying="XYZ", strike=50.0, dte=30, bid=1.00, ask=1.10,
             delta=-0.25, oi=500, exp: Optional[date] = None):
    """Return (raw_contract, snapshot_dict) for a put option."""
    exp = exp or (date.today() + timedelta(days=dte))
    sym = make_occ(underlying, exp, "P", strike)
    raw = FakeOptionContractRaw(sym, underlying, strike, exp, open_interest=oi)
    snap = {
        "latestQuote": {"bp": bid, "ap": ask},
        "greeks": {"delta": delta},
    }
    return raw, snap


class FakeBrokerClient:
    def __init__(self, account: Optional[FakeAccount] = None):
        self.account = account or FakeAccount()
        self.positions: List[FakePosition] = []
        self.stock_trades: Dict[str, float] = {}      # symbol -> price
        self.option_chain: Dict[str, List] = {}       # underlying -> [(raw, snap)]
        self.orders: Dict[str, FakeOrder] = {}
        self.submitted: List[FakeOrder] = []          # every submitted order
        self.option_sells: List[str] = []             # option symbols sold
        self.option_sell_attempts: List[str] = []     # attempted (incl. rejected)
        self.stock_buys: List[tuple] = []             # (symbol, qty)
        self.stock_sells: List[tuple] = []
        self.cancelled: List[str] = []
        # behaviors
        self.auto_fill = True                          # market orders fill instantly
        self.limit_fills = True                        # limit orders fill instantly
        self.raise_on_option_sell: Optional[Exception] = None
        self.raise_on_stock_sell: Optional[Exception] = None
        self.enforce_options_bp = False                # if True, reject option sells over BP
        self.sgov_sale_credits_bp = False              # if True, filled SGOV sells raise options BP (real Alpaca behavior)
        # sub-client used by closer/roller (client.trade_client.submit_order)
        self.trade_client = _FakeTradeClient(self)

    # ---- account / positions ----
    def get_account(self):
        return self.account

    def get_positions(self):
        return list(self.positions)

    # ---- market data ----
    def get_stock_latest_trade(self, symbols):
        if isinstance(symbols, str):
            symbols = [symbols]
        return {s: SimpleNamespace(price=p) for s, p in self.stock_trades.items()
                if s in symbols}

    def get_options_contracts(self, underlying_symbols, contract_type=None):
        out = []
        for u in underlying_symbols:
            for raw, _snap in self.option_chain.get(u, []):
                if contract_type is None or _occ_type(raw.symbol) == contract_type[0].upper():
                    out.append(raw)
        return out

    def get_option_snapshot(self, symbols):
        if isinstance(symbols, str):
            symbols = [symbols]
        out = {}
        for chain in self.option_chain.values():
            for raw, snap in chain:
                if raw.symbol in symbols:
                    out[raw.symbol] = snap
        return out

    # ---- orders ----
    def _submit_stock(self, symbol, qty, side):
        if side == "sell" and self.raise_on_stock_sell:
            raise self.raise_on_stock_sell
        o = FakeOrder(symbol, qty, side, "market",
                      status="filled" if self.auto_fill else "accepted")
        self.orders[o.id] = o
        self.submitted.append(o)
        (self.stock_sells if side == "sell" else self.stock_buys).append((symbol, qty))
        if (side == "sell" and symbol == "SGOV" and self.sgov_sale_credits_bp
                and o.status == "filled"):
            price = self.stock_trades.get("SGOV", 100.5)
            self.account.options_buying_power = (
                float(self.account.options_buying_power) + qty * price)
        return o

    def _submit_option_sell(self, symbol):
        self.option_sell_attempts.append(symbol)
        if self.raise_on_option_sell:
            raise self.raise_on_option_sell
        if self.enforce_options_bp:
            strike = _occ_strike(symbol)
            need = 100 * strike
            if float(self.account.options_buying_power) < need:
                raise Exception(
                    f'{{"code":40310000,"message":"insufficient options buying power '
                    f'for cash-secured put (required: {need}, available: '
                    f'{self.account.options_buying_power})"}}')
        o = FakeOrder(symbol, 1, "sell", "market",
                      status="filled" if self.auto_fill else "accepted")
        self.orders[o.id] = o
        self.submitted.append(o)
        self.option_sells.append(symbol)
        return o

    def market_buy(self, symbol, qty=1):
        return self._submit_stock(symbol, qty, "buy")

    def market_sell_qty(self, symbol, qty=1):
        return self._submit_stock(symbol, qty, "sell")

    def market_sell(self, symbol, qty=1):
        return self._submit_option_sell(symbol)

    def limit_sell(self, symbol, limit_price, qty=1):
        if self.raise_on_option_sell:
            raise self.raise_on_option_sell
        o = FakeOrder(symbol, qty, "sell", "limit", limit_price=limit_price,
                      status="filled" if self.limit_fills else "new",
                      filled_avg_price=limit_price if self.limit_fills else None)
        self.orders[o.id] = o
        self.submitted.append(o)
        if self.limit_fills:
            self.option_sells.append(symbol)
        return o

    def get_order(self, order_id):
        return self.orders[order_id]

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self.orders[order_id].status = "canceled"

    def liquidate_all_positions(self):
        self.positions = []

    # ---- helpers for tests ----
    def add_sgov(self, qty, price=100.50):
        self.positions.append(FakePosition("SGOV", qty, price, price))
        self.stock_trades.setdefault("SGOV", price)

    def add_put_position(self, underlying, strike, exp, qty=-1, entry=1.0,
                         current=0.5):
        sym = make_occ(underlying, exp, "P", strike)
        self.positions.append(FakePosition(sym, qty, entry, current,
                                           asset_class="us_option"))
        return sym


class _FakeTradeClient:
    """Used by closer.close_position / roller.roll_position via
    client.trade_client.submit_order(req) where req is an alpaca
    MarketOrderRequest — we duck-type on .symbol/.side."""

    def __init__(self, broker: FakeBrokerClient):
        self._b = broker

    def submit_order(self, req):
        symbol = getattr(req, "symbol", None)
        side = getattr(req, "side", None)
        side_s = str(side).lower()
        o = FakeOrder(symbol, getattr(req, "qty", 1), side_s, "market",
                      status="filled" if self._b.auto_fill else "accepted")
        self._b.orders[o.id] = o
        self._b.submitted.append(o)
        return o

    def get_order_by_id(self, order_id):
        return self._b.orders[order_id]

    def get_orders(self, filter=None):
        return [o for o in self._b.orders.values() if o.status in ("new", "accepted")]

    def get_all_positions(self):
        return self._b.get_positions()

    def get_account(self):
        return self._b.get_account()


def _occ_type(sym: str) -> str:
    import re
    m = re.match(r"^([A-Z]+)(\d{6})([PC])(\d{8})$", sym)
    return m.group(3) if m else "?"


def _occ_strike(sym: str) -> float:
    import re
    m = re.match(r"^([A-Z]+)(\d{6})([PC])(\d{8})$", sym)
    return int(m.group(4)) / 1000.0 if m else 0.0
