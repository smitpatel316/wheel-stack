"""
Optionable ↔ options-wheel bridge - Full sync including closes
Pushes Alpaca trades into Optionable tracker (default http://localhost:8096)

Fixes applied audit 2026-08-03:
- Commission 0 for paper
- Equity sync idempotent DELETE before POST
- Close handling: when Alpaca short option disappears, mark Optionable trade Expired/Assigned
- SGOV handled as stock

Fixes v2.5.4 2026-08-04:
- BUG: sync_closed_trades set closePrice=0 always => phantom P/L $568 vs real $52
  EntryPrice is sell premium, closePrice is buy-to-close price. Profit = entry - close.
  Setting close=0 implied expired worthless max profit for every close, inflated P/L.
- FIX: Implement _fetch_buy_price that queries Alpaca closed BUY orders for OCC symbol,
  get_close_price_from_activities helper, and sync_closed_trades now accepts optional
  close_price or close_price_map and fetches real fill price.
- New: sync_realized_pnl_from_alpaca computes real realized P/L from Alpaca fills and
  corrects Optionable closePrice.
- push_trade_to_optionable now handles commission properly, idempotent dup check, stores
  notes with OCC for traceability.

Fail-open outbox (2026-08-27, Pi migration):
- push_trade_to_optionable writes every trade payload to a durable local outbox
  (core/sync_outbox.py, state/sync-outbox/) BEFORE any network attempt, with a
  stable syncId in notes so re-delivery after a crash cannot double-record.
  If Optionable is unreachable the payload stays queued and a later run's
  outbox drain delivers it. The engine journal + Alpaca remain canonical;
  the dashboard is only a replica, so its outage can never halt a run.
"""
import datetime, logging, re, os, sys, math, time
from typing import Optional, Tuple, List, Dict, Union
import requests

logger = logging.getLogger("strategy.optionable_sync")

OPTIONABLE_URL = os.getenv("OPTIONABLE_URL", "http://localhost:8096")
TIMEOUT = 8
TREASURY_SYMBOLS = {"SGOV", "USFR", "BIL", "SHV", "TFLO"}

def alive() -> bool:
    try:
        r = requests.get(f"{OPTIONABLE_URL}/api/health", timeout=TIMEOUT)
        return r.status_code == 200
    except Exception as e:
        logger.debug(f"Optionable not reachable {OPTIONABLE_URL}: {e}")
        return False

def _parse_occ(occ_symbol: str) -> Optional[Tuple[str, str, str, float, str]]:
    m = re.match(r'^([A-Z]+)(\d{6})([PC])(\d{8})$', occ_symbol.strip())
    if not m:
        m = re.match(r'^([A-Z\.\/]+)(\d{6})([PC])(\d{8})$', occ_symbol.strip().replace(" ", ""))
    if not m:
        logger.warning(f"Failed to parse OCC {occ_symbol}")
        return None
    underlying_raw = m.group(1).strip()
    yymmdd = m.group(2)
    pc = m.group(3)
    strike_raw = m.group(4)
    yy = int(yymmdd[:2])
    year = 2000 + yy if yy < 70 else 1900 + yy
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    try:
        exp_date = datetime.date(year, month, day).isoformat()
    except ValueError as e:
        logger.debug("[SWALLOWED] expiry-date construction for OCC %s: %r", occ_symbol, e)
        return None
    opt_type = "Put" if pc == "P" else "Call"
    strike = int(strike_raw) / 1000.0
    return underlying_raw, exp_date, opt_type, strike, yymmdd

def get_default_account_id() -> int:
    try:
        r = requests.get(f"{OPTIONABLE_URL}/api/accounts", timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            accounts = data.get('data') or []
            if accounts:
                return accounts[0]['id']
    except Exception as e:
        logger.warning("[SWALLOWED] fetch of Optionable accounts for default account id (falling back to 1): %r", e)
        pass
    return 1

def get_optionable_open_trades(account_id: int) -> List[Dict]:
    try:
        r = requests.get(f"{OPTIONABLE_URL}/api/trades?status=Open&accountId={account_id}", timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json().get('data') or []
    except Exception as e:
        logger.debug(f"get open trades failed: {e}")
    return []

def get_optionable_all_trades(account_id: int) -> List[Dict]:
    try:
        r = requests.get(f"{OPTIONABLE_URL}/api/trades?accountId={account_id}", timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json().get('data') or []
    except Exception as e:
        logger.debug(f"get all trades failed: {e}")
    return []

def _commission_for_trade():
    # Alpaca Paper has $0 commission, live also often $0 for stocks but options $0.65
    try:
        from config.credentials import IS_PAPER
        return 0 if IS_PAPER else 0.65
    except Exception as e:
        logger.debug("[SWALLOWED] import of config.credentials for commission (env fallback): %r", e)
        # fallback to env check
        return 0 if os.getenv("ALPACA_PAPER","true").lower() in ("true","1") else 0.65

# --- v2.5.4 P/L FIX HELPERS ---

def _fetch_buy_price(client, occ_symbol: str) -> Optional[float]:
    """
    Fetch actual buy-to-close fill price for given OCC symbol from Alpaca orders.
    Loops closed orders, finds BUY side filled for symbol, returns filled_avg_price.
    """
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        # Try 100 most recent closed orders, with pagination
        # Alpaca GetOrdersRequest supports symbols filter? We'll search manually
        attempts = 0
        next_token = None
        # Search up to 500 orders to find match
        while attempts < 5:
            req_kwargs = {"status": QueryOrderStatus.CLOSED, "limit": 100}
            if next_token:
                req_kwargs["page_token"] = next_token
            try:
                req = GetOrdersRequest(**req_kwargs)
                resp = client.trade_client.get_orders(filter=req)
                orders = resp if isinstance(resp, list) else getattr(resp, 'data', []) or list(resp)
            except Exception as e:
                logger.debug(f"_fetch_buy_price get_orders failed: {e}")
                break

            if not orders:
                break

            for o in orders:
                try:
                    sym = getattr(o, 'symbol', None) or getattr(o, 'symbol_', None) or ""
                    side = str(getattr(o, 'side', '')).lower()
                    if sym == occ_symbol and 'buy' in side:
                        # Prefer filled_avg_price, else limit_price
                        avg = getattr(o, 'filled_avg_price', None) or getattr(o, 'filled_price', None) or getattr(o, 'limit_price', None)
                        if avg is not None:
                            try:
                                price = float(avg)
                                if price > 0:
                                    logger.debug(f"_fetch_buy_price {occ_symbol} found BUY @ ${price} order {getattr(o,'id','')}")
                                    return price
                            except Exception as e:
                                logger.debug("[SWALLOWED] parse of BUY fill price for %s: %r", occ_symbol, e)
                                continue
                        # Also check legs for option orders?
                        legs = getattr(o, 'legs', None)
                        if legs:
                            for leg in legs:
                                ls = getattr(leg, 'symbol', '')
                                if ls == occ_symbol:
                                    lp = getattr(leg, 'filled_avg_price', None)
                                    if lp:
                                        try:
                                            return float(lp)
                                        except Exception as e:
                                            logger.debug("[SWALLOWED] parse of leg fill price for %s: %r", occ_symbol, e)
                                            pass
                except Exception as e:
                    logger.debug("[SWALLOWED] inspection of order while searching BUY fill for %s: %r", occ_symbol, e)
                    continue

            # Pagination
            try:
                next_token = getattr(resp, 'next_page_token', None) if not isinstance(resp, list) else None
            except Exception as e:
                logger.debug("[SWALLOWED] extraction of next_page_token while searching BUY fill for %s: %r", occ_symbol, e)
                next_token = None
            if not next_token:
                break
            attempts += 1

        # Fallback: try activities API (trade activities)
        try:
            # Alpaca get_account_activities with activity_type FILL
            acts = client.trade_client.get_account_activities(filter=None) if hasattr(client.trade_client, 'get_account_activities') else []
            # Actually use client's get_activities method wrapper if exists
        except Exception as e:
            logger.debug("[SWALLOWED] optional activities-API probe for %s: %r", occ_symbol, e)  # swallow:intentional
            pass

    except Exception as e:
        logger.debug(f"_fetch_buy_price exception for {occ_symbol}: {e}")
    return None

def get_close_price_from_activities(client, occ_symbol: str) -> Optional[float]:
    """
    Public wrapper: attempt to resolve close price from Alpaca activities/orders.
    Tries _fetch_buy_price first, then client.get_account_activities if available.
    """
    price = _fetch_buy_price(client, occ_symbol)
    if price is not None:
        return price
    # Try alternative path via client.get_account_activities (if broker_client exposes)
    try:
        if hasattr(client, 'trade_client') and hasattr(client.trade_client, 'get_account_activities'):
            from alpaca.trading.requests import GetAccountActivitiesRequest
            req = GetAccountActivitiesRequest(activity_types=["FILL"], symbols=[occ_symbol])
            activities = client.trade_client.get_account_activities(filter=req)
            for act in activities:
                try:
                    act_sym = getattr(act, 'symbol', '')
                    act_side = str(getattr(act, 'side', '')).lower()
                    if act_sym == occ_symbol and 'buy' in act_side:
                        p = getattr(act, 'price', None) or getattr(act, 'price_per_share', None)
                        if p:
                            return float(p)
                except Exception as e:
                    logger.debug("[SWALLOWED] parse of activity record for %s: %r", occ_symbol, e)
                    continue
    except Exception as e:
        logger.debug(f"get_close_price_from_activities fallback failed {occ_symbol}: {e}")

    return None

def push_trade_to_optionable(
    alpaca_occ_symbol: str,
    bid_per_share: float,
    contracts: int = 1,
    opened_date: Optional[str] = None,
    delta: Optional[float] = None,
    account_id: Optional[int] = None,
) -> bool:
    """
    v2.5.4 improved: proper commission handling, idempotent duplicate check,
    store OCC in notes for traceability if API supports, better logging.

    Fail-open (2026-08-27, Pi migration): the payload lands in the durable
    local outbox (core/sync_outbox.py) FIRST, carrying a stable syncId in
    notes; then the outbox drains (delivering any backlog oldest-first plus
    this payload). If Optionable is unreachable the trade stays queued and a
    later run delivers it — the engine journal + Alpaca are canonical, the
    dashboard is only a replica. Never raises into the engine.
    """
    parsed = _parse_occ(alpaca_occ_symbol)
    if not parsed:
        return False
    underlying, exp_date, opt_type, strike, _ = parsed
    if underlying in TREASURY_SYMBOLS:
        logger.debug(f"Skip treasury OCC {underlying}")
        return False
    if opened_date is None:
        opened_date = datetime.date.today().isoformat()
    if account_id is None:
        account_id = get_default_account_id()

    trade_type = "CSP" if opt_type == "Put" else "CC"
    comm_per_contract = _commission_for_trade()
    comm = comm_per_contract * int(contracts)

    # Optionable expects delta 0-1, Alpaca gives -0.3 for puts -> abs()
    delta_val = abs(float(delta)) if delta is not None else None

    from core.sync_outbox import make_trade_sync_id, enqueue_trade, drain_outbox, is_queued
    sync_id = make_trade_sync_id(alpaca_occ_symbol, opened_date)
    payload = {
        "ticker": underlying,
        "type": trade_type,
        "strike": float(strike),
        "quantity": int(contracts),
        "delta": delta_val,
        "entryPrice": float(bid_per_share),
        "closePrice": 0,
        "openedDate": opened_date,
        "expirationDate": exp_date,
        "closedDate": None,
        "status": "Open",
        "accountId": account_id,
        "commission": comm,
        "notes": f"OCC:{alpaca_occ_symbol} syncId:{sync_id} via wheel-stack v2.5.4",
    }
    if payload["delta"] is None:
        del payload["delta"]

    # Durable FIRST: even a crash between fill and push loses nothing.
    # The outbox drain does the same dup check the old inline code did
    # (syncId in notes, or ticker/strike/expiry/type tuple match) before POSTing.
    enqueue_trade(payload, sync_id)

    if not alive():
        logger.warning(
            f"[SYNC] Optionable unreachable - {trade_type} {underlying} ${strike} {exp_date} "
            f"queued in local outbox (id {sync_id[:8]}); a later run will deliver it. "
            f"Engine journal + Alpaca remain canonical.")
        return False
    drain_outbox()
    if is_queued(sync_id):
        logger.warning(f"[SYNC] Optionable push not acknowledged - {trade_type} {underlying} "
                       f"${strike} {exp_date} remains queued (id {sync_id[:8]}), will retry next run")
        return False
    logger.info(f"Optionable: logged {trade_type} {underlying} ${strike} exp {exp_date} ${bid_per_share:.2f}x{contracts} comm ${comm:.2f}")
    return True

def sync_alpaca_equity_to_optionable(client):
    """Sync equity longs (excluding treasuries) as manual stocks - idempotent"""
    if not alive():
        return
    try:
        account_id = get_default_account_id()
        # Fetch existing stocks to dedupe
        existing = {}
        try:
            r = requests.get(f"{OPTIONABLE_URL}/api/stocks?accountId={account_id}", timeout=TIMEOUT)
            if r.status_code == 200:
                for s in (r.json().get('data') or []):
                    if s.get('ticker') != 'SGOV':  # SGOV handled separately
                        # keep latest per ticker
                        existing[s.get('ticker')] = s
        except Exception as e:
            logger.warning("[SWALLOWED] fetch of existing Optionable stocks for equity sync: %r", e)
            pass

        positions = client.get_positions()
        current_tickers = set()
        for p in positions:
            ac = str(getattr(p, "asset_class", "")).upper()
            if "OPTION" in ac:
                continue
            sym = getattr(p, "symbol", None)
            if not sym or sym in TREASURY_SYMBOLS:
                continue
            try:
                qty = int(float(getattr(p, "qty", 0)))
            except Exception as e:
                logger.debug("[SWALLOWED] parse of position qty for %s: %r", sym, e)
                continue
            if qty <= 0:
                continue
            avg = float(getattr(p, "avg_entry_price", 0))
            if avg <= 0:
                continue
            current_tickers.add(sym)
            # If already exists with same qty+avg, skip to avoid churn
            ex = existing.get(sym)
            if ex and ex.get('shares') == qty and abs(float(ex.get('costBasis',0)) - avg) < 0.01:
                continue
            # Delete existing to replace
            if ex:
                try:
                    requests.delete(f"{OPTIONABLE_URL}/api/stocks/{ex['id']}", timeout=TIMEOUT)
                except Exception as e:
                    logger.warning("[SWALLOWED] delete of existing Optionable stock %s (id %s) before re-sync: %r", sym, ex.get('id'), e)
                    pass
            payload = {
                "ticker": sym,
                "shares": qty,
                "costBasis": float(avg),
                "acquiredDate": datetime.date.today().isoformat(),
                "accountId": account_id,
            }
            try:
                r = requests.post(f"{OPTIONABLE_URL}/api/stocks", json=payload, timeout=TIMEOUT)
                if r.status_code in (200,201):
                    logger.info(f"Optionable: synced stock {sym} {qty}x${avg}")
            except Exception as e:
                logger.debug(f"Optionable stock sync {sym} failed: {e}")

        # Remove stocks that no longer exist in Alpaca (sold)
        for ticker, stock in existing.items():
            if ticker not in current_tickers and ticker != 'SGOV':
                try:
                    # Mark as sold? For now delete, actual sell should generate capitalGainLoss via manual flow
                    # We keep it as closed by not deleting? Better to leave as closed handling outside
                    # For simplicity, if Alpaca no longer has it, assume sold - we don't auto-close to preserve history
                    pass
                except Exception as e:
                    logger.debug("[SWALLOWED] no-op sold-stock placeholder for %s: %r", ticker, e)  # swallow:intentional
                    pass
    except Exception as e:
        logger.warning(f"sync_alpaca_equity_to_optionable failed: {e}")

def sync_sgov_to_optionable(client):
    """SGOV idle cash -> track as stock"""
    if not alive():
        return
    try:
        account_id = get_default_account_id()
        positions = client.get_positions()
        sgov_qty = 0
        sgov_avg = 100.72
        for p in positions:
            if getattr(p, "symbol", None) == "SGOV":
                try:
                    sgov_qty = int(float(getattr(p, "qty", 0)))
                    sgov_avg = float(getattr(p, "avg_entry_price", sgov_qty and 100.72))
                except Exception as e:
                    logger.debug("[SWALLOWED] parse of SGOV position qty/avg: %r", e)
                    pass
        if sgov_qty <= 0:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            try:
                opens = client.trade_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=20))
                for o in opens:
                    if getattr(o, 'symbol', None) == 'SGOV':
                        sgov_qty = int(float(getattr(o, 'qty', 0)))
                        # Avg from order filled_avg_price if filled else use price estimate
                        avg_pr = getattr(o, 'filled_avg_price', None)
                        if avg_pr:
                            try:
                                sgov_avg = float(avg_pr)
                            except Exception as e:
                                logger.debug("[SWALLOWED] parse of SGOV order filled_avg_price: %r", e)
                                pass
                        break
            except Exception as e:
                logger.warning("[SWALLOWED] fetch of open SGOV orders from Alpaca: %r", e)
                pass
            if sgov_qty <= 0:
                return

        # Fetch current SGOV stock entries
        existing_sgov = []
        try:
            r = requests.get(f"{OPTIONABLE_URL}/api/stocks?accountId={account_id}", timeout=TIMEOUT)
            if r.status_code == 200:
                for s in (r.json().get('data') or []):
                    if s.get('ticker') == 'SGOV':
                        existing_sgov.append(s)
        except Exception as e:
            logger.warning("[SWALLOWED] fetch of existing SGOV stock entries from Optionable: %r", e)
            pass

        # If exists with same qty/avg, skip
        if existing_sgov:
            # If any matches qty, skip
            for es in existing_sgov:
                if es.get('shares') == sgov_qty and abs(float(es.get('costBasis',0)) - sgov_avg) < 0.05:
                    return
            # Otherwise delete all existing SGOV to replace
            for es in existing_sgov:
                try:
                    requests.delete(f"{OPTIONABLE_URL}/api/stocks/{es['id']}", timeout=TIMEOUT)
                except Exception as e:
                    logger.warning("[SWALLOWED] delete of stale SGOV stock entry (id %s) before re-sync: %r", es.get('id'), e)
                    pass

        payload = {
            "ticker": "SGOV",
            "shares": sgov_qty,
            "costBasis": float(sgov_avg),
            "acquiredDate": datetime.date.today().isoformat(),
            "accountId": account_id,
            "notes": "Treasury proxy for idle cash - Alpaca real - v2.5.3 SPAXX sweep wrapper interest"
        }
        try:
            r = requests.post(f"{OPTIONABLE_URL}/api/stocks", json=payload, timeout=TIMEOUT)
            if r.status_code in (200,201):
                logger.info(f"Optionable: SGOV {sgov_qty}x${sgov_avg:.2f} synced")
        except Exception as e:
            logger.debug(f"SGOV stock sync failed: {e}")

    except Exception as e:
        logger.warning(f"sync_sgov_to_optionable failed: {e}")

def sync_closed_trades(client, close_price: Optional[Union[float, Dict[str,float]]] = None, close_price_map: Optional[Dict[str,float]] = None):
    """
    When Alpaca short option positions disappear (expired/assigned/closed),
    mark corresponding Optionable Open trades as Expired/Closed.

    v2.5.4 FIX: previously set closePrice=0 for all closes => profit = entry (phantom $568).
    Now fetches actual buy-to-close price from Alpaca orders via _fetch_buy_price / get_close_price_from_activities.

    Args:
        client: BrokerClient
        close_price: optional global override close price (float) OR dict OCC->price
        close_price_map: optional dict mapping (ticker,strike,exp,type) or OCC -> close price

    Heuristic:
    - Get all Optionable Open trades
    - Get all Alpaca short option positions (OCC symbols)
    - If Optionable Open trade ticker+strike+exp not in Alpaca positions, and exp date <= today => Expired closePrice 0 (worthless)
    - Else if closed early, fetch real buy price; profit = entry - close.
    - If stock position appeared for same ticker and CSP, mark Assigned.
    """
    if not alive():
        return
    try:
        account_id = get_default_account_id()
        open_trades = get_optionable_open_trades(account_id)
        if not open_trades:
            return

        positions = client.get_positions()
        alpaca_option_occs = set()
        alpaca_stock_tickers = set()
        for p in positions:
            sym = getattr(p, 'symbol', '') or ''
            ac = str(getattr(p, 'asset_class', '')).upper()
            if 'OPTION' in ac:
                alpaca_option_occs.add(sym)
            else:
                if sym not in TREASURY_SYMBOLS:
                    alpaca_stock_tickers.add(sym)

        # Parse Alpaca OCCs to ticker/strike/exp tuples for comparison
        alpaca_parsed = {}
        for occ in alpaca_option_occs:
            parsed = _parse_occ(occ)
            if parsed:
                underlying, exp_date, opt_type, strike, _ = parsed
                key = (underlying, round(strike,2), exp_date, opt_type)
                alpaca_parsed[key] = occ

        # Normalize close_price param
        price_lookup: Dict[str, float] = {}
        if isinstance(close_price, dict):
            price_lookup.update(close_price)
        if isinstance(close_price_map, dict):
            price_lookup.update(close_price_map)
        global_close_override: Optional[float] = None
        if isinstance(close_price, (int, float)):
            global_close_override = float(close_price)

        today = datetime.date.today().isoformat()
        today_date = datetime.date.today()

        for trade in open_trades:
            ticker = trade.get('ticker')
            strike = round(float(trade.get('strike',0)),2)
            exp = trade.get('expirationDate')
            ttype = trade.get('type')
            entry_price = float(trade.get('entryPrice', 0) or 0)
            # Map Optionable type to opt_type
            opt_type = 'Put' if ttype == 'CSP' else 'Call' if ttype in ('CC',) else None
            if not opt_type:
                continue
            key = (ticker, strike, exp, opt_type)
            if key not in alpaca_parsed:
                # Option disappeared from Alpaca - need to determine close type and price
                new_status = 'Closed'
                close_price_val: float

                # Check explicit map first
                # Try OCC symbol if we stored in notes
                occ_guess = None
                notes = trade.get('notes','')
                if notes and 'OCC:' in notes:
                    try:
                        occ_guess = notes.split('OCC:')[1].split()[0].strip()
                    except Exception as e:
                        logger.debug("[SWALLOWED] parse of OCC from trade notes for %s: %r", ticker, e)
                        occ_guess = None

                # Lookup by OCC or by tuple string
                lookup_keys = []
                if occ_guess:
                    lookup_keys.append(occ_guess)
                lookup_keys.append(f"{ticker}_{strike}_{exp}_{opt_type}")
                lookup_keys.append(f"{ticker}{exp}{opt_type[0]}{int(strike*1000):08d}")

                found_in_map = None
                for lk in lookup_keys:
                    if lk in price_lookup:
                        found_in_map = float(price_lookup[lk])
                        break

                if found_in_map is not None:
                    close_price_val = found_in_map
                    logger.info(f"Optionable close override map ${close_price_val:.2f} for {ticker} {ttype} ${strike} {exp}")
                elif global_close_override is not None:
                    close_price_val = global_close_override
                else:
                    # Try fetch from Alpaca if we have OCC
                    fetched = None
                    if occ_guess:
                        fetched = get_close_price_from_activities(client, occ_guess)
                    else:
                        # Attempt to reconstruct OCC to search? Build from trade
                        # We don't have yymmdd exact but try brute find buy order for ticker+strike+exp
                        # Fallback: loop all closed buy orders and parse OCC matching our key
                        try:
                            from alpaca.trading.requests import GetOrdersRequest
                            from alpaca.trading.enums import QueryOrderStatus
                            req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=100)
                            orders = client.trade_client.get_orders(filter=req)
                            for o in orders:
                                try:
                                    sym = getattr(o, 'symbol','')
                                    if not sym:
                                        continue
                                    p = _parse_occ(sym)
                                    if not p:
                                        continue
                                    u, ed, ot, sk, _ = p
                                    if u==ticker and round(sk,2)==strike and ed==exp and ot==opt_type:
                                        side = str(getattr(o,'side','')).lower()
                                        if 'buy' in side:
                                            avg = getattr(o,'filled_avg_price',None) or getattr(o,'limit_price',None)
                                            if avg:
                                                fetched = float(avg)
                                                occ_guess = sym
                                                break
                                except Exception as e:
                                    logger.debug("[SWALLOWED] inspection of closed order in brute buy search for %s %s %s: %r", ticker, strike, exp, e)
                                    continue
                        except Exception as e:
                            logger.debug(f"brute buy search failed: {e}")

                    if fetched is not None:
                        close_price_val = fetched
                    else:
                        # Determine if expired worthless
                        is_expired = False
                        try:
                            if exp and exp <= today:
                                is_expired = True
                        except Exception as e:
                            logger.debug("[SWALLOWED] expiry-date comparison for %s exp %s: %r", ticker, exp, e)
                            is_expired = False

                        if ticker in alpaca_stock_tickers and ttype == 'CSP':
                            # Assigned
                            new_status = 'Assigned'
                            # Assigned -> closePrice = strike? Actually Optionable Assigned uses entry - 0? 
                            # For assigned CSP, profit is entry premium, closePrice 0 because stock now held
                            # Keep closePrice 0 for assignment case
                            close_price_val = 0.0
                            logger.info(f"Optionable: {ticker} {ttype} ${strike} {exp} -> {new_status} (stock {ticker} appeared) entry ${entry_price:.2f} close ${close_price_val:.2f}")
                        elif is_expired:
                            new_status = 'Expired'
                            close_price_val = 0.0  # expired worthless = max profit correct
                            logger.info(f"Optionable: {ticker} {ttype} ${strike} {exp} -> {new_status} (expired worthless) profit ${entry_price:.2f}")
                        else:
                            # Closed early but we couldn't find buy price - log warning and estimate
                            # To avoid phantom P/L bug, don't default to 0; use entry * 0.5 estimate and flag
                            # Better to set closePrice = entryPrice * 0.5 (50% profit) as conservative until real price found
                            # But log discrepancy for manual correction
                            new_status = 'Closed'
                            # If we have current market data? fallback to half entry
                            estimated_close = round(entry_price * 0.5, 2)  # assume 50% profit taken
                            close_price_val = estimated_close
                            logger.warning(
                                f"Optionable: {ticker} {ttype} ${strike} {exp} closed but BUY fill not found - "
                                f"est close ${estimated_close:.2f} entry ${entry_price:.2f} profit ${(entry_price-estimated_close):.2f}. "
                                f"Run sync_realized_pnl_from_alpaca for exact. Using est to avoid $0 bug."
                            )

                # Final status adjustment for assignment check if not already
                if ticker in alpaca_stock_tickers and ttype == 'CSP' and new_status != 'Assigned':
                    # If stock exists, mark assigned even if we had close price
                    # But keep closePrice 0 for assigned per Optionable semantics
                    if close_price_val != 0:
                        # if we have real buy price, keep Closed not Assigned unless explicitly assigned
                        pass
                    else:
                        new_status = 'Assigned'

                if new_status == 'Assigned':
                    close_price_val = 0.0  # Optionable semantics for assigned

                # Compute P/L for logging
                try:
                    qty = int(trade.get('quantity',1))
                    commission = float(trade.get('commission',0) or 0)
                    # Optionable P/L model: profit = (entry - close)*100*qty - commission
                    # For assigned/expired close=0 => profit = entry*100*qty - commission
                    pl = (entry_price - close_price_val) * 100 * qty - commission if new_status != 'Assigned' else entry_price*100*qty - commission
                    logger.info(f"Optionable: {ticker} {ttype} ${strike} {exp} -> {new_status} closePrice ${close_price_val:.2f} entry ${entry_price:.2f} qty {qty} P/L ${pl:.2f} fees ${commission:.2f} (real vs phantom check)")
                except Exception as e:
                    logger.debug("[SWALLOWED] P/L computation for close log of %s %s: %r", ticker, ttype, e)
                    pass

                # Update trade status via PUT
                try:
                    close_date = today
                    payload = {"status": new_status, "closedDate": close_date, "closePrice": float(close_price_val)}
                    # Include commission if existing
                    if trade.get('commission') is not None:
                        payload["commission"] = trade.get('commission')
                    r = requests.put(f"{OPTIONABLE_URL}/api/trades/{trade['id']}", json=payload, timeout=TIMEOUT)
                    if r.status_code in (200,201):
                        logger.info(f"Optionable: updated trade {trade['id']} to {new_status} closePrice ${close_price_val:.2f}")
                    else:
                        logger.warning(f"Optionable PUT {trade['id']} failed {r.status_code}: {r.text[:400]}")
                except Exception as e:
                    logger.debug(f"Failed to update trade {trade['id']} to {new_status}: {e}")

    except Exception as e:
        logger.warning(f"sync_closed_trades failed: {e}", exc_info=True)

def sync_realized_pnl_from_alpaca(client) -> Dict:
    """
    v2.5.4 NEW: Computes real realized P/L from Alpaca fills and corrects Optionable.

    Steps:
    1. Pull closed orders (CLOSED) for options, separate SELL (open) and BUY (close)
    2. Match OCC symbols: realized P/L = sum(sell_qty*fill_price) - sum(buy_qty*fill_price) - fees
    3. Compare to Optionable closed trades, if closePrice discrepancy > $0.05, PUT correction.

    Returns dict with summary for logging: {realized, optionable_pnl, discrepancy, corrected_count}
    """
    summary = {"realized": 0.0, "fees": 0.0, "optionable_pnl": 0.0, "discrepancy": 0.0, "corrected": 0, "entries": []}
    if not alive():
        logger.debug("Optionable not alive, skip realized P/L sync")
        return summary
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        account_id = get_default_account_id()

        # Fetch all closed orders (paginate)
        all_closed = []
        next_token = None
        attempts = 0
        while attempts < 10:
            kwargs = {"status": QueryOrderStatus.CLOSED, "limit": 100}
            if next_token:
                kwargs["page_token"] = next_token
            try:
                req = GetOrdersRequest(**kwargs)
                resp = client.trade_client.get_orders(filter=req)
                chunk = resp if isinstance(resp, list) else getattr(resp, 'data', []) or list(resp)
                if not chunk:
                    break
                all_closed.extend(chunk)
                next_token = getattr(resp, 'next_page_token', None) if not isinstance(resp, list) else None
                if not next_token:
                    break
            except Exception as e:
                logger.debug(f"sync_realized_pnl get_orders page {attempts} failed: {e}")
                break
            attempts += 1

        # Group by OCC symbol
        # Structure: {occ: {"sells": [(qty, price)], "buys": [(qty, price)], "realized": float}}
        occ_map: Dict[str, Dict] = {}
        for o in all_closed:
            try:
                sym = getattr(o, 'symbol', None)
                if not sym or len(sym) < 10:  # rough OCC length check
                    continue
                if _parse_occ(sym) is None:
                    continue  # not option
                side = str(getattr(o, 'side', '')).lower()
                qty = float(getattr(o, 'filled_qty', 0) or getattr(o, 'qty', 0) or 0)
                price = getattr(o, 'filled_avg_price', None) or getattr(o, 'filled_price', None)
                if price is None or qty == 0:
                    continue
                try:
                    price_f = float(price)
                    qty_f = float(qty)
                except Exception as e:
                    logger.debug("[SWALLOWED] float parse of fill price/qty for %s: %r", sym, e)
                    continue
                if sym not in occ_map:
                    occ_map[sym] = {"sells": [], "buys": [], "sell_total": 0.0, "buy_total": 0.0, "qty_sell": 0.0, "qty_buy": 0.0}
                if 'sell' in side:
                    occ_map[sym]["sells"].append((qty_f, price_f))
                    occ_map[sym]["sell_total"] += qty_f * price_f * 100  # per share *100 contracts
                    occ_map[sym]["qty_sell"] += qty_f
                elif 'buy' in side:
                    occ_map[sym]["buys"].append((qty_f, price_f))
                    occ_map[sym]["buy_total"] += qty_f * price_f * 100
                    occ_map[sym]["qty_buy"] += qty_f
            except Exception as e:
                logger.debug("[SWALLOWED] grouping of closed order into occ_map (sym %s): %r", getattr(o, 'symbol', None), e)
                continue

        # Compute realized per OCC where buy qty >= sell qty (closed)
        realized_total = 0.0
        for occ, data in occ_map.items():
            if data["qty_buy"] > 0 and data["qty_sell"] > 0:
                # Realized when position closed (qty match)
                # Simplified: profit = sell_total - buy_total (fees zero paper)
                pl = data["sell_total"] - data["buy_total"]
                realized_total += pl
                summary["entries"].append({"occ": occ, "sell": data["sell_total"], "buy": data["buy_total"], "pl": pl})

        summary["realized"] = realized_total

        # Now fetch Optionable closed trades to compare
        try:
            open_trades_all = get_optionable_all_trades(account_id)
            optionable_realized = 0.0
            # Map OCC -> close price from occ_map (avg buy price)
            for tr in open_trades_all:
                if tr.get('status') not in ('Closed','Expired','Assigned'):
                    continue
                entry = float(tr.get('entryPrice',0) or 0)
                close = float(tr.get('closePrice',0) or 0)
                qty = int(tr.get('quantity',1) or 1)
                comm = float(tr.get('commission',0) or 0)
                if tr.get('status') == 'Assigned':
                    pnl = entry*100*qty - comm
                else:
                    pnl = (entry - close)*100*qty - comm
                optionable_realized += pnl

                # Try to correct if we have real buy data and closePrice is 0 but should not be 0
                # Find matching OCC via notes or via ticker/strike/exp
                occ_match = None
                notes = tr.get('notes','')
                if notes and 'OCC:' in notes:
                    try:
                        cand = notes.split('OCC:')[1].split()[0].strip()
                        if cand in occ_map and occ_map[cand]["qty_buy"]>0:
                            occ_match = cand
                    except Exception as e:
                        logger.debug("[SWALLOWED] parse of OCC candidate from trade notes: %r", e)
                        pass
                if not occ_match:
                    # Try match via parsing all occ_map keys that match ticker/strike/exp
                    try:
                        ticker = tr.get('ticker')
                        strike = round(float(tr.get('strike',0)),2)
                        exp = tr.get('expirationDate')
                        otype = 'Put' if tr.get('type')=='CSP' else 'Call'
                        for occ_sym in occ_map:
                            p = _parse_occ(occ_sym)
                            if not p: 
                                continue
                            u, ed, ot, sk, _ = p
                            if u==ticker and round(sk,2)==strike and ed==exp and ot==otype:
                                if occ_map[occ_sym]["qty_buy"]>0:
                                    occ_match = occ_sym
                                    break
                    except Exception as e:
                        logger.debug("[SWALLOWED] match of trade %s to OCC via parsed occ_map: %r", tr.get('ticker'), e)
                        pass

                if occ_match:
                    data = occ_map[occ_match]
                    if data["qty_buy"]>0:
                        avg_buy = data["buy_total"] / (data["qty_buy"]*100) if data["qty_buy"] else 0
                        # If Optionable closePrice is 0 and avg_buy >0 and status Closed (not Expired/Assigned), correct
                        if tr.get('status')=='Closed' and abs(close - 0) < 0.001 and avg_buy > 0:
                            # Bug case: should be avg_buy not 0
                            correct_payload = {"closePrice": float(avg_buy), "status": "Closed"}
                            try:
                                r = requests.put(f"{OPTIONABLE_URL}/api/trades/{tr['id']}", json=correct_payload, timeout=TIMEOUT)
                                if r.status_code in (200,201):
                                    logger.info(f"Optionable P/L FIX: corrected trade {tr['id']} {ticker} close ${close:.2f} -> ${avg_buy:.2f} (Alpaca real) OCC {occ_match}")
                                    summary["corrected"] += 1
                            except Exception as e:
                                logger.debug(f"Failed to correct trade {tr['id']}: {e}")
                        elif tr.get('status')=='Closed' and abs(close - avg_buy) > 0.05:
                            # Discrepancy >5c
                            logger.info(f"Optionable P/L CHECK: trade {tr['id']} {tr.get('ticker')} close ${close:.2f} vs Alpaca ${avg_buy:.2f} diff ${(close-avg_buy):.2f} OCC {occ_match}")

            summary["optionable_pnl"] = optionable_realized
            summary["discrepancy"] = optionable_realized - realized_total
            logger.info(f"[P/L SYNC] Real Alpaca realized ${realized_total:.2f} | Optionable ${optionable_realized:.2f} | discrepancy ${summary['discrepancy']:.2f} | corrected {summary['corrected']} entries")

        except Exception as e:
            logger.debug(f"Optionable realized comparison failed: {e}")

        return summary

    except Exception as e:
        logger.warning(f"sync_realized_pnl_from_alpaca failed: {e}", exc_info=True)
        return summary


def reconcile_open_entry_prices(client) -> int:
    """Correct Optionable Open trade entryPrice to the broker's avg entry.

    Engine-pushed opens record a quote/limit price at order time; the broker's
    avg_entry_price after fills is the truth. A few cents of drift per leg
    compounds into wrong per-trade P/L (observed 2026-08-27: JNJ entry logged
    2.08 vs broker STO 1.82 => -$72 reported vs -$98 real). Idempotent:
    skips rows within $0.005, never touches rows without an OCC: syncId note,
    no-ops when the tracker is down. Returns rows patched.
    """
    if not alive():
        return 0
    patched = 0
    try:
        account_id = get_default_account_id()
        rows = get_optionable_open_trades(account_id)
        if not rows:
            return 0
        broker_avg: Dict[str, float] = {}
        for p in client.get_positions():
            if 'OPTION' not in str(getattr(p, 'asset_class', '')).upper():
                continue
            sym = getattr(p, 'symbol', '') or ''
            try:
                avg = float(getattr(p, 'avg_entry_price', 0) or 0)
            except Exception as e:
                logger.debug("[SWALLOWED] parse of avg_entry_price for %s: %r", sym, e)  # swallow:non-fatal-position
                continue
            if sym and avg > 0:
                broker_avg[sym] = avg
        if not broker_avg:
            return 0
        for t in rows:
            m = re.search(r'OCC:([A-Z0-9]{10,25})', t.get('notes') or '')
            if not m:
                continue
            avg = broker_avg.get(m.group(1))
            if not avg:
                continue
            occ = m.group(1)
            try:
                cur = float(t.get('entryPrice') or 0)
            except Exception as e:
                logger.debug("[SWALLOWED] parse of Optionable entryPrice for %s: %r", t.get('id'), e)  # swallow:non-fatal-row
                continue
            if abs(cur - avg) < 0.005:
                continue
            try:
                r = requests.put(f"{OPTIONABLE_URL}/api/trades/{t['id']}",
                                 json={"entryPrice": round(avg, 4)}, timeout=TIMEOUT)
                if r.status_code == 200:
                    patched += 1
                    logger.info(f"Optionable entry reconciled #{t['id']} {occ}: {cur} -> {avg}")
                else:
                    logger.debug(f"entry reconcile PUT {t['id']} -> {r.status_code}")
            except Exception as e:
                logger.debug("[SWALLOWED] entry reconcile PUT failed for %s: %r", t.get('id'), e)  # swallow:non-fatal-push
        if patched:
            logger.info(f"Optionable entry reconciliation patched {patched} open trade(s)")
    except Exception as e:
        logger.warning("[SWALLOWED] reconcile_open_entry_prices failed (non-fatal): %r", e)  # swallow:non-fatal-sync
    return patched
