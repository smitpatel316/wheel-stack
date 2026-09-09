"""Robinhood broker adapter — BrokerClient-compatible money paths.

Routes ORDERS, POSITIONS, and ACCOUNT to Robinhood via rh_order_client
(the Robinhood MCP write path). Market DATA (option chains, quotes, stock
trades) stays on the Alpaca data clients: quotes are quotes, and the engine's
screening models are built on that feed. The OCC option symbol is the bridge
between the two (parsed -> RH option_id via get_option_instruments).

Fail-closed:
- Construction requires live=True explicitly AND env RH_LIVE_ORDERS=true.
  Either missing -> RHNotLiveError. There is no paper trading on Robinhood,
  so this adapter can ONLY move real money; the double gate is deliberate.
- liquidate_all_positions() raises: --fresh-start is refused on Robinhood.
- Every order goes through review_option_order first; a non-clean review
  aborts the order (enforced inside rh_order_client).
- dry_run=True: full path through review, never places (RH_DRY_RUN=true).

Position/account shapes returned here mirror the Alpaca SDK attributes the
engine reads (symbol/qty/avg_entry_price/market_value/current_price/
unrealized_pl/asset_class on positions; cash/equity/buying_power/
options_buying_power/portfolio_value on account).
"""

import logging
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "robinhood-mcp"))

from rh_order_client import (  # noqa: E402
    RHOrderError,
    _call_tool,
    dry_run_review,
    find_option_id,
    get_agentic_account,
    get_option_order,
    get_portfolio,
    get_positions as _rh_option_positions,
    order_fill_summary,
    parse_occ,
    place_option_order,
    wait_for_fill,
    cancel_option_order as _rh_cancel,
)

logger = logging.getLogger(__name__)

OCC_RE = re.compile(r"^([A-Za-z]+)(\d{6})([PC])(\d{8})$")


def _to_occ(underlying: str, expiration: str, strike: float, option_type: str) -> str:
    """Build an OCC symbol the engine's parse_option_symbol accepts."""
    yymmdd = expiration.replace("-", "")[2:]
    cp = "C" if option_type.lower().startswith("c") else "P"
    return f"{underlying.upper()}{yymmdd}{cp}{int(round(strike * 1000)):08d}"


def _is_occ(symbol: str) -> bool:
    return bool(OCC_RE.match(str(symbol or "").strip().upper()))


class _RHOrderView:
    """Alpaca-order-shaped view over an RH order dict."""

    def __init__(self, order: dict, symbol: str = ""):
        self._order = order
        self.symbol = symbol
        self.id = order.get("id")

    @property
    def status(self) -> str:
        return str(self._order.get("state") or "").lower()

    @property
    def filled_avg_price(self):
        return order_fill_summary(self._order).get("avg_fill_price")

    @property
    def filled_qty(self):
        return order_fill_summary(self._order).get("filled_qty")


class _RHTradeClientShim:
    """Minimal trade_client surface for roller.py / closer.py.

    Translates Alpaca order-request objects into RH legs. Wheel usage only:
    BUY on an OCC symbol is always buy_to_close; SELL on an OCC symbol is
    always sell_to_open. Anything else raises.
    """

    def __init__(self, adapter: "RobinhoodBrokerClient"):
        self._a = adapter

    def submit_order(self, req):
        symbol = getattr(req, "symbol", "")
        qty = int(getattr(req, "qty", 1) or 1)
        if not _is_occ(symbol):
            raise RHOrderError(f"RH shim: non-OCC symbol {symbol!r} not supported")
        side = str(getattr(req, "side", "")).lower()
        limit_price = getattr(req, "limit_price", None)
        if "buy" in side:
            return self._a._place(symbol, "buy", "close", qty,
                                  limit_price=float(limit_price) if limit_price else None)
        if "sell" in side:
            return self._a._place(symbol, "sell", "open", qty,
                                  limit_price=float(limit_price) if limit_price else None)
        raise RHOrderError(f"RH shim: unsupported side {side!r}")

    def get_order_by_id(self, order_id):
        return self._a.get_order(order_id)

    def get_orders(self, *args, **kwargs):
        raise RHOrderError("RH shim: filtered get_orders not implemented; "
                           "use adapter.get_recent_option_orders() (add when needed)")


class RobinhoodBrokerClient:
    # Identity hook for engine code that must behave differently per broker
    # (duck-typed via getattr so the Alpaca path needs no import). Paper-only
    # machinery — the Optionable new-trade push, the T+1 FundingQueue — keys
    # off this to stay away from the RH account.
    broker_name = "robinhood"

    def __init__(self, data_client=None, live: bool = False, dry_run: bool = False):
        if not live or os.getenv("RH_LIVE_ORDERS", "false").lower() not in ("1", "true", "yes"):
            raise RHOrderError(
                "RobinhoodBrokerClient requires live=True AND RH_LIVE_ORDERS=true. "
                "Robinhood has no paper trading; this adapter only moves real money."
            )
        self._data = data_client
        self._dry_run = dry_run or os.getenv("RH_DRY_RUN", "false").lower() in ("1", "true", "yes")
        self._option_id_cache: dict = {}
        self._trade_client = _RHTradeClientShim(self)
        mode = "DRY-RUN (review only, never places)" if self._dry_run else "LIVE"
        logger.warning(f"[RH] RobinhoodBrokerClient initialized in {mode} mode")

    @property
    def trade_client(self):
        return self._trade_client

    @property
    def dry_run(self) -> bool:
        """Public read of the RH_DRY_RUN gate: review-only, never places."""
        return self._dry_run

    # ---------------------------------------------------------- internal
    def _option_id(self, occ_symbol: str) -> str:
        key = occ_symbol.strip().upper()
        if key not in self._option_id_cache:
            occ = parse_occ(key)
            self._option_id_cache[key] = find_option_id(
                occ["underlying"], occ["expiration"], occ["strike"], occ["type"])
        return self._option_id_cache[key]

    def _place(self, occ_symbol: str, side: str, effect: str, qty: int,
               limit_price: float | None = None) -> _RHOrderView:
        option_id = self._option_id(occ_symbol)
        legs = [{"option_id": option_id, "side": side,
                 "position_effect": effect, "ratio_quantity": 1}]
        logical_key = f"{occ_symbol}:{side}:{effect}:{qty}"
        order_type = "limit" if limit_price else "market"
        if self._dry_run:
            out = dry_run_review(legs, qty, order_type, limit_price)
            logger.warning(f"[RH] DRY-RUN {side} {effect} {occ_symbol} x{qty} "
                           f"review_clean={out['review_clean']}")
            return _RHOrderView({"id": f"dry-run-{logical_key}", "state": "dry_run",
                                 "legs": []}, symbol=occ_symbol)
        order = place_option_order(legs, qty, order_type, limit_price,
                                   logical_key=logical_key, live=True)
        return _RHOrderView(order, symbol=occ_symbol)

    # ---------------------------------------------------------- orders
    def market_sell(self, symbol, qty=1):
        return self._place(symbol, "sell", "open", int(qty))

    def market_sell_qty(self, symbol, qty=1):
        return self.market_sell(symbol, qty)

    def market_buy(self, symbol, qty=1):
        return self._place(symbol, "buy", "close", int(qty))

    def limit_sell(self, symbol, limit_price, qty=1):
        return self._place(symbol, "sell", "open", int(qty), limit_price=float(limit_price))

    def get_order(self, order_id):
        if str(order_id).startswith("dry-run-"):
            return _RHOrderView({"id": order_id, "state": "dry_run", "legs": []})
        order = get_option_order(str(order_id))
        if order is None:
            raise RHOrderError(f"RH order {order_id} not found on agentic account")
        return _RHOrderView(order)

    def cancel_order(self, order_id):
        if str(order_id).startswith("dry-run-"):
            return None
        return _rh_cancel(str(order_id), live=True)

    def wait_for_fill(self, order_id, timeout_s: float = 30.0):
        if str(order_id).startswith("dry-run-"):
            return {"state": "dry_run", "filled_qty": 0, "avg_fill_price": None}
        return wait_for_fill(str(order_id), timeout_s=timeout_s)

    def liquidate_all_positions(self):
        raise RHOrderError("liquidate_all_positions refused on Robinhood: "
                           "--fresh-start is not allowed on the live RH account")

    # ---------------------------------------------------------- account
    def get_account(self):
        data = get_portfolio()
        # Defensive: portfolio shape varies; try several layouts. Some fields
        # (e.g. buying_power) arrive as nested {"buying_power": "123.00", ...}
        # objects rather than flat strings.
        def _num(*keys):
            def _coerce(v):
                if isinstance(v, dict):
                    for nk in ("buying_power", "amount", "value", "unleveraged_buying_power"):
                        if v.get(nk) is not None:
                            return _coerce(v[nk])
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    logger.debug("[SWALLOWED] non-numeric nested portfolio value %r", v)
                    return None
            for k in keys:
                v = data.get(k)
                if v is None and isinstance(data.get("portfolio"), dict):
                    v = data["portfolio"].get(k)
                if v is not None:
                    n = _coerce(v)
                    if n is not None:
                        return n
                    logger.debug("[SWALLOWED] non-numeric portfolio field %r=%r", k, v)
            return 0.0
        cash = _num("cash", "cash_available", "withdrawable_cash")
        equity = _num("equity", "portfolio_value", "market_value", "total_value")
        bp = _num("buying_power", "option_buying_power", "options_buying_power")
        return SimpleNamespace(
            cash=cash, equity=equity or (cash + _num("market_value",)),
            buying_power=bp, options_buying_power=bp,
            portfolio_value=equity or cash,
            raw=data,
        )

    # ---------------------------------------------------------- positions
    def get_positions(self):
        out = []
        for p in _rh_option_positions():
            occ = self._position_occ(p)
            if not occ:
                continue
            qty = self._qty(p)
            out.append(SimpleNamespace(
                symbol=occ,
                qty=qty,
                avg_entry_price=self._num(p, "average_price", "avg_entry_price", "average_cost"),
                market_value=self._num(p, "market_value"),
                current_price=self._num(p, "current_price", "mark_price", "average_price"),
                unrealized_pl=self._num(p, "unrealized_pl", "unrealized_gain_loss"),
                asset_class="US_OPTION",
                raw=p,
            ))
        try:
            acct = get_agentic_account()
            data = _call_tool("get_equity_positions", {
                "account_number": acct["account_number"], "nonzero": True},
                label="get_equity_positions")
            for p in data.get("positions") or data.get("equity_positions") or []:
                qty = self._qty(p)
                out.append(SimpleNamespace(
                    symbol=str(p.get("symbol") or p.get("chain_symbol") or "").upper(),
                    qty=qty,
                    avg_entry_price=self._num(p, "average_buy_price", "average_price"),
                    market_value=self._num(p, "market_value"),
                    current_price=self._num(p, "current_price", "last_trade_price"),
                    unrealized_pl=self._num(p, "unrealized_pl"),
                    asset_class="US_EQUITY",
                    raw=p,
                ))
        except Exception as e:
            logger.debug("[SWALLOWED] RH equity positions fetch failed: %r", e)
        return out

    @staticmethod
    def _num(p: dict, *keys) -> float:
        for k in keys:
            try:
                v = p.get(k)
                if v is not None:
                    return float(v)
            except (TypeError, ValueError):
                logger.debug("[SWALLOWED] non-numeric position field %r=%r", k, p.get(k))
                continue
        return 0.0

    @staticmethod
    def _qty(p: dict) -> int:
        for k in ("quantity", "qty"):
            try:
                v = p.get(k)
                if v is not None:
                    return int(float(v))
            except (TypeError, ValueError):
                logger.debug("[SWALLOWED] non-numeric qty field %r=%r", k, p.get(k))
                continue
        return 0

    def _position_occ(self, p: dict) -> str | None:
        # Prefer a native OCC symbol if the API returns one.
        for k in ("occ_symbol", "option_symbol", "symbol"):
            v = p.get(k)
            if v and _is_occ(str(v)):
                return str(v).upper()
        # Otherwise construct from components.
        und = p.get("chain_symbol") or p.get("underlying") or p.get("symbol")
        exp = p.get("expiration_date") or p.get("expiration")
        strike = p.get("strike_price")
        otype = p.get("type") or p.get("option_type") or p.get("put_call")
        try:
            if und and exp and strike is not None and otype:
                return _to_occ(str(und), str(exp)[:10], float(strike), str(otype))
        except (TypeError, ValueError):
            logger.debug("[SWALLOWED] bad OCC components und=%r exp=%r strike=%r", und, exp, strike)
        logger.warning(f"[RH] could not build OCC for position: {json_keys(p)}")
        return None

    # ---------------------------------------------------------- data (delegated)
    def get_options_contracts(self, underlying_symbols, contract_type=None):
        self._require_data("get_options_contracts")
        return self._data.get_options_contracts(underlying_symbols, contract_type)

    def get_option_snapshot(self, symbol):
        self._require_data("get_option_snapshot")
        return self._data.get_option_snapshot(symbol)

    def get_stock_latest_trade(self, symbol):
        self._require_data("get_stock_latest_trade")
        return self._data.get_stock_latest_trade(symbol)

    def _require_data(self, what: str):
        if self._data is None:
            raise RHOrderError(f"RH adapter: {what} needs a data_client (Alpaca) — none configured")


def json_keys(p: dict) -> str:
    return ",".join(sorted(p.keys())[:12])
