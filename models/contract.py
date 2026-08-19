"""Reconstructed models.contract (missing from git; lived only on the Pi).

Contract wraps an Alpaca option contract + its market snapshot into the shape
the strategy code expects: symbol, underlying, strike, dte, bid/ask, delta, oi.

Handles both snapshot representations defensively:
- raw dicts (Alpaca JSON: latestQuote {ap, bp}, greeks {delta})
- alpaca-py model objects (latest_quote {ask_price, bid_price}, greeks {delta})
"""
from datetime import date, datetime
import logging

log = logging.getLogger(__name__)


def _get(obj, *keys):
    """Fetch the first present key/attribute from a dict or model object."""
    if obj is None:
        return None
    for k in keys:
        if isinstance(obj, dict):
            if obj.get(k) is not None:
                return obj[k]
        else:
            v = getattr(obj, k, None)
            if v is not None:
                return v
    return None


def _to_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError) as e:
        log.debug("[SWALLOWED] contract field %r not float-parseable: %r", v, e)
        return None


class Contract:
    def __init__(self, symbol=None, underlying=None, strike=None, expiration=None,
                 dte=None, bid_price=None, ask_price=None, delta=None, oi=None):
        self.symbol = symbol
        self.underlying = underlying
        self.strike = strike
        self.expiration = expiration
        self.dte = dte
        self.bid_price = bid_price
        self.ask_price = ask_price
        self.delta = delta
        self.oi = oi

    @classmethod
    def from_contract_snapshot(cls, contract, snap):
        symbol = _get(contract, "symbol")
        underlying = _get(contract, "underlying_symbol", "underlying")
        strike = _to_float(_get(contract, "strike_price", "strike"))
        expiration = _get(contract, "expiration_date", "expiration")
        oi_raw = _get(contract, "open_interest", "oi")
        try:
            oi = int(oi_raw) if oi_raw is not None else None
        except (TypeError, ValueError) as e:
            log.debug("[SWALLOWED] open_interest %r not int-parseable for %s: %r", oi_raw, symbol, e)
            oi = None

        # DTE from expiration (date or ISO string)
        dte = None
        exp = expiration
        if isinstance(exp, str):
            try:
                exp = datetime.strptime(exp[:10], "%Y-%m-%d").date()
            except ValueError as e:
                log.debug("[SWALLOWED] expiration %r not ISO-parseable for %s: %r", exp, symbol, e)
                exp = None
        if isinstance(exp, datetime):
            exp = exp.date()
        if isinstance(exp, date):
            dte = (exp - date.today()).days

        quote = _get(snap, "latestQuote", "latest_quote")
        bid = _to_float(_get(quote, "bp", "bid_price", "bidPrice"))
        ask = _to_float(_get(quote, "ap", "ask_price", "askPrice"))

        greeks = _get(snap, "greeks")
        delta = _to_float(_get(greeks, "delta"))

        return cls(symbol=symbol, underlying=underlying, strike=strike,
                   expiration=str(expiration) if expiration is not None else None,
                   dte=dte, bid_price=bid, ask_price=ask, delta=delta, oi=oi)

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "underlying": self.underlying,
            "strike": self.strike,
            "expiration": self.expiration,
            "dte": self.dte,
            "bid_price": self.bid_price,
            "ask_price": self.ask_price,
            "delta": self.delta,
            "oi": self.oi,
        }

    def __repr__(self):
        return (f"Contract({self.symbol} strike={self.strike} dte={self.dte} "
                f"bid={self.bid_price} ask={self.ask_price} delta={self.delta} oi={self.oi})")
