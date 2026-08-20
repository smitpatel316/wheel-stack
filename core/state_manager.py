import json
import os

from .utils import parse_option_symbol
from alpaca.trading.enums import AssetClass
import logging

log = logging.getLogger(__name__)

# v2.6: max defensive rolls per position lineage ("BAC:P"), then let it ride to
# assignment/expiry. Community consensus: a position rolled 1-2 times without
# improvement should stop rolling (rolls out of hope compound losses).
MAX_ROLLS_PER_LINEAGE = 2
ROLL_COUNTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "roll_counts.json")


def load_roll_counts(path=ROLL_COUNTS_PATH):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        # swallow:intentional - first run before any roll has happened
        log.debug("[SWALLOWED] no roll-count file at %s yet (first run)", path)
        return {}
    except Exception as e:
        log.warning("roll counts unreadable at %s, starting fresh: %r", path, e)
        return {}


def save_roll_counts(counts, path=ROLL_COUNTS_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(counts, f, indent=1)
    os.replace(tmp, path)


def prune_roll_counts(counts, states):
    """Drop lineages whose position is gone (expired, closed, or assigned).

    A short-put lineage ends at assignment (state flips to long_shares); the
    covered-call phase is a separate "SYM:C" lineage with its own count.
    """
    alive = set()
    for sym, st in states.items():
        t = st.get("type")
        if t == "short_put":
            alive.add(f"{sym}:P")
        elif t == "short_call":
            alive.add(f"{sym}:C")
    return {k: v for k, v in counts.items() if k in alive}


# Treasury proxies treated as cash collateral, excluded from wheel risk
TREASURY_SYMBOLS = {"SGOV", "USFR", "BIL", "SHV", "TFLO"}


def calculate_risk(positions):
    risk = 0
    for p in positions:
        sym = getattr(p, "symbol", "") or ""
        if sym in TREASURY_SYMBOLS:
            continue
        if p.asset_class == AssetClass.US_EQUITY:
            risk += float(p.avg_entry_price) * abs(int(p.qty))
        elif p.asset_class == AssetClass.US_OPTION:
            _, option_type, strike_price = parse_option_symbol(p.symbol)
            if option_type == 'P':
                risk += 100 * strike_price * abs(int(p.qty))

    return risk

def calculate_exposures(positions):
    """Return put_exposure, long_stock (excl treasuries), risk"""
    put_exp = 0
    long_stock = 0
    risk = calculate_risk(positions)
    for p in positions:
        sym = getattr(p, "symbol", "") or ""
        if sym in TREASURY_SYMBOLS:
            continue
        if p.asset_class == AssetClass.US_EQUITY:
            long_stock += float(p.avg_entry_price) * abs(int(p.qty))
        elif p.asset_class == AssetClass.US_OPTION:
            try:
                _, otype, strike = parse_option_symbol(p.symbol)
                if otype == 'P':
                    put_exp += 100 * float(strike) * abs(int(float(p.qty)))
            except Exception as e:
                log.warning("[SWALLOWED] exposure calc: unparseable option position %s skipped: %r", getattr(p, 'symbol', '?'), e)
                pass
    return put_exp, long_stock, risk

def update_state(all_positions):    
    """
    Given the current positions, return a state dictionary describing where in the wheel each symbol is.
    """

    state = {}

    for p in all_positions:
        if p.asset_class == AssetClass.US_EQUITY:
            if int(p.qty) <= 0:
                raise ValueError(f"Only long stock positions allowed! Got {p.symbol} with qty {p.qty}")

            underlying = p.symbol
            if underlying in state:
                if state[underlying]["type"] != "short_call_awaiting_stock":
                    raise ValueError(f"Unexpected state for {underlying}: {state[underlying]}")
                state[underlying]["type"] = "short_call"
            else:
                state[underlying] = {"type": "long_shares", "price": float(p.avg_entry_price), "qty": int(p.qty)}

        elif p.asset_class == AssetClass.US_OPTION:
            if int(p.qty) >= 0:
                raise ValueError(f"Only short option positions allowed! Got {p.symbol} with qty {p.qty}")

            underlying, option_type, _ = parse_option_symbol(p.symbol)

            if underlying in state:
                if not (state[underlying]["type"] == "long_shares" and option_type == 'C'):
                    raise ValueError(f"Unexpected state for {underlying}: {state[underlying]} with option {option_type}")
                state[underlying]["type"] = "short_call"
            else:
                if option_type == "C":
                    state[underlying] = {"type": "short_call_awaiting_stock", "price": None}
                elif option_type == "P":
                    state[underlying] = {"type": "short_put", "price": None}
                else:
                    raise ValueError(f"Unknown option type: {option_type}")

    # Final validation
    for underlying, st in state.items():
        if st["type"] not in {"short_put", "long_shares", "short_call"}:
            raise ValueError(f"Invalid final state for {underlying}: {st}")
        
    return state