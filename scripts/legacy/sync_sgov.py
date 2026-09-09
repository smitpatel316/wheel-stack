#!/usr/bin/env python3
"""
SGOV <-> Alpaca <-> Optionable sync - REAL Alpaca execution path
Wheeler legacy removed, primary tracker is Optionable (default http://localhost:8096)
"""
import math, datetime, sys, os, time, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from config.credentials import ALPACA_API_KEY, ALPACA_SECRET_KEY, IS_PAPER
from core.broker_client import BrokerClient
from core.optionable_sync import alive as optionable_alive, sync_sgov_to_optionable
import requests, logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sgov")

OPTIONABLE_URL = os.getenv("OPTIONABLE_URL", "http://localhost:8096")
TOTAL_CAPITAL = 100_000
TREASURY_SYMBOLS = {"SGOV", "USFR", "BIL", "SHV", "TFLO"}

def get_sgov_price_fallback(client):
    try:
        latest = client.get_stock_latest_trade("SGOV")
        trade = latest.get("SGOV") if isinstance(latest, dict) else None
        price = getattr(trade, 'price', None) if trade else None
        if price is None and isinstance(trade, dict):
            price = float(trade.get('price', 0))
        if price:
            return float(price)
    except Exception as e:
        log.warning(f"SGOV price fetch failed: {e}")
    return 100.72

def calc_risk_excluding_treasury(positions):
    from alpaca.trading.enums import AssetClass
    from core.utils import parse_option_symbol
    risk = 0
    put_exp = 0
    long_stock = 0
    for p in positions:
        sym = getattr(p, 'symbol', '') or ''
        if sym in TREASURY_SYMBOLS:
            continue
        if p.asset_class == AssetClass.US_EQUITY:
            v = float(p.avg_entry_price) * abs(int(float(p.qty)))
            risk += v
            long_stock += v
        elif p.asset_class == AssetClass.US_OPTION:
            try:
                _, otype, strike = parse_option_symbol(p.symbol)
                if otype == 'P':
                    v = 100 * float(strike) * abs(int(float(p.qty)))
                    risk += v
                    put_exp += v
            except Exception as e:
                log.warning("[SWALLOWED] risk calc: unparseable option position %s skipped: %r", getattr(p, 'symbol', '?'), e)
                pass
    return risk, put_exp, long_stock

def get_alpaca_sgov_qty(positions):
    for p in positions:
        if getattr(p, 'symbol', '') == 'SGOV':
            try:
                return int(float(getattr(p, 'qty', 0)))
            except Exception as e:
                log.debug("[SWALLOWED] SGOV qty parse failed, treating as 0: %r", e)
                return 0
    return 0

def main():
    client = BrokerClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY, paper=IS_PAPER)
    positions = client.get_positions()
    acct = client.get_account()
    risk, put_exp, long_stock = calc_risk_excluding_treasury(positions)
    sgov_qty_alpaca = get_alpaca_sgov_qty(positions)
    price = get_sgov_price_fallback(client)

    if len(sys.argv) > 1 and sys.argv[1] == "dynamic":
        idle = TOTAL_CAPITAL - risk
        if put_exp == 0 and long_stock == 0:
            target_cash = min(50000, max(0, idle))
        else:
            target_cash = max(0, idle)
    elif len(sys.argv) > 1 and sys.argv[1].lstrip('-').isdigit():
        target_cash = int(sys.argv[1])
    else:
        target_cash = 50000

    import math as m
    target_shares = m.floor(target_cash / price) if target_cash >= price else 0
    diff = target_shares - sgov_qty_alpaca

    print(f"[SGOV] price ${price:.2f} Alpaca SGOV {sgov_qty_alpaca} target {target_shares} ( ${target_cash} ) diff {diff} | put ${put_exp:.0f} long(excl SGOV) ${long_stock:.0f} risk ${risk:.0f} cash ${float(acct.cash):.0f}")

    # Open-order guard
    try:
        from alpaca.trading.requests import GetOrdersRequest as GOR
        from alpaca.trading.enums import QueryOrderStatus as QOS
        open_orders = client.trade_client.get_orders(filter=GOR(status=QOS.OPEN, limit=50))
        sgov_open_buy = sum(int(float(o.qty)) for o in open_orders if getattr(o,'symbol','')=='SGOV' and str(getattr(o,'side','')).lower().find('buy')>=0)
        if sgov_open_buy > 0 and diff > 0:
            print(f"[ALPACA] Existing open BUY SGOV {sgov_open_buy} - skip duplicate")
            diff = 0
    except Exception as e:
        log.debug(f"Open order check failed: {e}")

    if diff > 0:
        print(f"[ALPACA] Buying {diff} SGOV @ ~${price:.2f}")
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        try:
            req = MarketOrderRequest(symbol="SGOV", qty=diff, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
            order = client.trade_client.submit_order(req)
            print(f"[ALPACA] Order {order.id} status {order.status}")
        except Exception as e:
            log.warning("[SWALLOWED] SGOV buy order of %d shares failed: %r", diff, e)
            print(f"[ALPACA] Order failed: {e}")
    elif diff < 0:
        print(f"[ALPACA] Selling {abs(diff)} SGOV")
        try:
            client.market_sell_qty("SGOV", abs(diff))
        except Exception as e:
            log.warning("[SWALLOWED] SGOV sell of %d shares failed: %r", abs(diff), e)
            print(f"[ALPACA] Sell failed: {e}")
    else:
        print("[ALPACA] SGOV at target, no order needed")

    # Sync to Optionable (primary)
    if optionable_alive():
        try:
            sync_sgov_to_optionable(client)
            # After fill, stock API shows SGOV
        except Exception as e:
            log.warning("[SWALLOWED] SGOV Optionable sync push failed: %r", e)
            print(f"Optionable sync failed: {e}")

    # Print final allocation
    try:
        import requests
        r = requests.get(f"{OPTIONABLE_URL}/api/stocks", timeout=5).json()
        print(f"[OPTIONABLE] Stocks: {[(x['ticker'], x['shares'], x['costBasis']) for x in r.get('data',[])]}")
    except Exception as e:
        log.debug("[SWALLOWED] final Optionable stocks readout failed (display only): %r", e)
        pass

if __name__ == "__main__":
    main()
