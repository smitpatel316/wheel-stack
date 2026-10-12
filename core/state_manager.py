import json
import math
import os

from datetime import datetime, timezone

from .utils import parse_option_symbol
from alpaca.trading.enums import AssetClass
import logging

log = logging.getLogger(__name__)

# v2.6: max defensive rolls per position lineage ("BAC:P"), then let it ride to
# assignment/expiry. Community consensus: a position rolled 1-2 times without
# improvement should stop rolling (rolls out of hope compound losses).
MAX_ROLLS_PER_LINEAGE = 2
ROLL_COUNTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "roll_counts.json")


def _quarantine_corrupt_roll_counts(path, err):
    """Preserve a bad roll-counts file for forensics and reset LOUDLY.

    2026-09-09: never silently discard the roll cap. A corrupt file means a
    previous write died mid-dump; a quiet {} reset here lets an
    already-twice-rolled position roll again, compounding losses the cap
    exists to prevent (mirrors the d045074 strategy_log.json fix).
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    corrupt_backup = f"{path}.corrupt-{ts}UTC"
    try:
        os.replace(path, corrupt_backup)
    except Exception as be:
        log.error("[SWALLOWED] roll-count backup of corrupt %s failed: %r", path, be)
    log.error("[SWALLOWED] roll counts %s unreadable (%r) - moved to %s, "
              "cap reset to {} (was: risk control weakened, investigate)",
              path, err, corrupt_backup)
    return {}


def _sanitize_roll_counts(raw, path):
    """Fail-closed cleanup of a parsed-but-wrong-shaped roll-counts file.

    Valid JSON with the wrong shape used to sail past the corrupt-file
    handler in load_roll_counts(): a top-level list crashed
    prune_roll_counts (.items) and killed the whole roll phase for the run;
    a string count crashed the `n >= MAX_ROLLS_PER_LINEAGE` check the same
    way; a negative count silently weakened the cap (it takes
    MAX - value + 1 increments to reach the cap). Sanitize per entry:
    malformed keys (can never match a real "SYM:P"/"SYM:C" lineage lookup)
    are dropped loudly; malformed or out-of-range values clamp to
    MAX_ROLLS_PER_LINEAGE so the cap fails CLOSED, never open.
    """
    if not isinstance(raw, dict):
        return _quarantine_corrupt_roll_counts(
            path, TypeError(f"roll counts must be a JSON object, got {type(raw).__name__}"))
    clean = {}
    for key, value in raw.items():
        parts = str(key).split(":")
        if len(parts) != 2 or not parts[0].isalpha() or parts[1] not in ("P", "C"):
            log.error("[SWALLOWED] roll counts %s: dropping malformed lineage key %r "
                      "(expected 'SYM:P'/'SYM:C')", path, key)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or (isinstance(value, float) and not math.isfinite(value)) \
                or value != int(value) or not 0 <= value <= MAX_ROLLS_PER_LINEAGE:
            log.error("[SWALLOWED] roll counts %s: lineage %s has invalid count %r - "
                      "clamping to cap %d (fail-closed; investigate)",
                      path, key, value, MAX_ROLLS_PER_LINEAGE)
            clean[key] = MAX_ROLLS_PER_LINEAGE
        else:
            clean[key] = int(value)
    return clean


def load_roll_counts(path=ROLL_COUNTS_PATH):
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        # swallow:intentional - first run before any roll has happened
        log.debug("[SWALLOWED] no roll-count file at %s yet (first run)", path)
        return {}
    except Exception as e:
        return _quarantine_corrupt_roll_counts(path, e)
    return _sanitize_roll_counts(raw, path)


def save_roll_counts(counts, path=ROLL_COUNTS_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Per-PID tmp name: two overlapping runs (cron + retry) sharing one
    # ".tmp" path could interleave writes and replace a torn file into
    # place - the exact mid-write corruption load_roll_counts guards
    # against. fsync before/after the rename so a VM crash cannot lose a
    # committed increment or resurrect a half-flushed tmp as the file.
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(counts, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as e:
        log.debug("[SWALLOWED] roll-count dir fsync failed for %s (rename already committed): %r", path, e)


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