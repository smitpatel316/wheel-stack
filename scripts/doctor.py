#!/usr/bin/env python3
"""wheel doctor — pre-flight health check for the whole stack.

Checks, in order:
  1. Config & credentials  (env-overridable params, key presence, paper/live consistency)
  2. Broker connectivity   (Alpaca account reachable, ACTIVE, no margin debit)
  3. Optionable sync       (dashboard API reachable)
  4. Data-source fallbacks (Finnhub ping, Alpha Vantage ping — rate-limit tolerant)

Exit code 0 = all green (warnings allowed), 1 = at least one hard failure.
Run it before every unattended stretch and before any go-live decision.
"""
import logging
import os
import sys

logger = logging.getLogger("doctor")

# Make repo imports work when invoked directly (python scripts/doctor.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_results = []


def report(status, check, msg):
    _results.append((status, check, msg))
    icon = {PASS: "✓", WARN: "!", FAIL: "✗"}[status]
    print(f"[{icon}] {check}: {msg}")


def check_config():
    from config import params
    from config import credentials as cred

    if not cred.ALPACA_API_KEY or not cred.ALPACA_SECRET_KEY:
        report(FAIL, "config", "ALPACA_API_KEY / ALPACA_SECRET_KEY missing (set in .env or environment)")
        return
    report(PASS, "config", "Alpaca credentials present")

    # Paper keys start with PK, live keys with AK — catch a paper/live mismatch
    # before it can place real orders or confuse a paper run.
    prefix = cred.ALPACA_API_KEY[:2]
    if cred.IS_PAPER and prefix == "AK":
        report(FAIL, "config", "IS_PAPER=true but ALPACA_API_KEY looks LIVE (starts 'AK') — refusing to trust this config")
    elif not cred.IS_PAPER and prefix == "PK":
        report(FAIL, "config", "IS_PAPER=false but ALPACA_API_KEY looks PAPER (starts 'PK')")
    else:
        report(PASS, "config", f"IS_PAPER={cred.IS_PAPER} matches key type ({prefix}…)")

    for name in ("FINNHUB_API_KEY", "ALPHA_VANTAGE_API_KEY"):
        if getattr(cred, name):
            report(PASS, "config", f"{name} present")
        else:
            report(WARN, "config", f"{name} missing — related data source will rely on fallbacks")

    report(PASS, "config",
           f"effective params: MAX_RISK={params.MAX_RISK} MIN_PREMIUM={params.MIN_PREMIUM} "
           f"SCORE_MIN={params.SCORE_MIN} SGOV_ENABLED={params.SGOV_ENABLED}")
    wl = params.load_watchlist()
    report(PASS, "config", f"watchlist: {len(wl)} symbol(s) from {params.watchlist_source()} ({', '.join(wl[:8])}{'…' if len(wl) > 8 else ''})")
    if params.SGOV_ENABLED:
        report(WARN, "config", "SGOV sweep is ON — expected only on the Alpaca paper account; must be OFF for Robinhood/live")


def check_broker():
    from config import credentials as cred
    from config import params
    from config.credentials import IS_PAPER
    from core.broker_client import BrokerClient
    try:
        client = BrokerClient(api_key=cred.ALPACA_API_KEY, secret_key=cred.ALPACA_SECRET_KEY, paper=IS_PAPER)
        acct = client.get_account()
    except Exception as e:
        logger.debug("[SWALLOWED] broker check failed: %r", e)
        report(FAIL, "broker", f"cannot reach Alpaca: {e}")
        return

    status = getattr(acct, "status", "?")
    if str(status) == "ACTIVE" or getattr(status, "value", None) == "ACTIVE":
        report(PASS, "broker", f"account ACTIVE ({'paper' if IS_PAPER else 'LIVE'})")
    else:
        report(FAIL, "broker", f"account status is {status}, not ACTIVE")

    cash = float(getattr(acct, "cash", 0) or 0)
    equity = float(getattr(acct, "equity", 0) or 0)
    opt_bp = float(getattr(acct, "options_buying_power", 0) or 0)
    if cash < 0:
        report(FAIL, "broker", f"MARGIN DEBIT: cash is ${cash:,.2f} — the wheel must never carry a margin debit")
    else:
        report(PASS, "broker", f"cash ${cash:,.0f} / equity ${equity:,.0f} / options BP ${opt_bp:,.0f} (no margin debit)")

    try:
        trade = client.get_stock_latest_trade("SPY")
        report(PASS, "broker", "market data feed reachable (SPY latest trade ok)" if trade else "market data feed returned empty for SPY")
    except Exception as e:
        logger.debug("[SWALLOWED] market data feed check failed: %r", e)
        report(WARN, "broker", f"trading API ok but market data feed failed: {e}")

    # If the sweep is disabled, the account should not still be sitting in SGOV.
    if not params.SGOV_ENABLED:
        try:
            sgov = [p for p in client.get_positions() if getattr(p, "symbol", "") == "SGOV"]
            if sgov:
                report(WARN, "broker", f"SGOV_ENABLED=false but account still holds {sgov[0].qty} SGOV — unwind it for a clean cash-only run")
        except Exception as e:
            logger.debug("[SWALLOWED] SGOV position check failed: %r", e)
            report(WARN, "broker", f"could not verify SGOV position: {e}")


def check_optionable():
    from core import optionable_sync
    url = optionable_sync.OPTIONABLE_URL
    if optionable_sync.alive():
        report(PASS, "optionable", f"dashboard reachable at {url}")
    else:
        report(FAIL, "optionable", f"dashboard NOT reachable at {url} — trades would not sync; start it (./wheel start) or set OPTIONABLE_URL")


def check_data_sources():
    from config import credentials as cred
    from core import data_fallbacks

    # Finnhub: real ping through the same fallback fetcher the engine uses.
    if cred.FINNHUB_API_KEY:
        try:
            data = data_fallbacks.fetch_overview_finnhub("AAPL")
            if data and data.get("PERatio"):
                report(PASS, "data", "Finnhub reachable (AAPL metrics ok)")
            else:
                report(WARN, "data", "Finnhub reachable but returned no metrics for AAPL")
        except Exception as e:
            logger.debug("[SWALLOWED] Finnhub ping failed: %r", e)
            report(WARN, "data", f"Finnhub ping failed: {data_fallbacks._redact(e, cred.FINNHUB_API_KEY)}")
    else:
        report(WARN, "data", "Finnhub key missing — fundamentals fallback unavailable")

    # Alpha Vantage: free tier is 25 req/day, so tolerate rate-limit responses;
    # only a hard error / invalid key is a failure.
    if cred.ALPHA_VANTAGE_API_KEY:
        try:
            import requests
            r = requests.get("https://www.alphavantage.co/query",
                             params={"function": "GLOBAL_QUOTE", "symbol": "IBM",
                                     "apikey": cred.ALPHA_VANTAGE_API_KEY}, timeout=15)
            body = r.json()
            if "Global Quote" in body:
                report(PASS, "data", "Alpha Vantage reachable (IBM quote ok)")
            elif "Information" in body or "Note" in body:
                report(WARN, "data", "Alpha Vantage rate-limited right now (expected on free tier); fallbacks cover it")
            elif "Error Message" in body:
                report(WARN, "data", "Alpha Vantage rejected the request — check ALPHA_VANTAGE_API_KEY")
            else:
                report(WARN, "data", f"Alpha Vantage unexpected response: {str(body)[:120]}")
        except Exception as e:
            logger.debug("[SWALLOWED] Alpha Vantage ping failed: %r", e)
            report(WARN, "data", f"Alpha Vantage ping failed: {e} (fallbacks cover it)")
    else:
        report(WARN, "data", "Alpha Vantage key missing — primary fundamentals source down, Finnhub fallback only")


def main():
    print("wheel doctor — pre-flight check\n" + "-" * 40)
    for fn in (check_config, check_broker, check_optionable, check_data_sources):
        try:
            fn()
        except Exception as e:
            logger.debug("[SWALLOWED] doctor check crashed: %r", e)
            report(FAIL, fn.__name__, f"check crashed: {e}")

    print("-" * 40)
    fails = [r for r in _results if r[0] == FAIL]
    warns = [r for r in _results if r[0] == WARN]
    if fails:
        print(f"✗ {len(fails)} FAIL, {len(warns)} WARN — do NOT run unattended until failures are fixed:")
        for _, check, msg in fails:
            print(f"    - {check}: {msg}")
        return 1
    print(f"✓ all hard checks pass ({len(warns)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
