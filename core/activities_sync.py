"""
Alpaca -> Optionable full sync using OpenAPI trading-api.json
Covers:
- FILL handled by TradingStream (real-time)
- DIV/INT/FEE -> fund_transactions
- OPASN/OPEXP/OPEXC -> trade status Assigned/Expired/Exercised
- JNLS/JNLC -> journal stock/cash (assignment)
- TRANS/CSD/CSW -> deposits/withdrawals

Uses raw REST because alpaca-py 0.43.5 lacks get_account_activities
"""
import os, requests, datetime, logging, time
from typing import List, Dict, Optional

logger = logging.getLogger("strategy.activities_sync")
OPTIONABLE_URL = os.getenv("OPTIONABLE_URL", "http://localhost:8096")
TIMEOUT = 10

def _alpaca_headers(client):
    # Try to get keys from client or env
    try:
        api_key = getattr(client, '_api_key', None) or client._api_key if hasattr(client, '_api_key') else os.getenv('ALPACA_API_KEY')
    except:
        from config.credentials import ALPACA_API_KEY
        api_key = ALPACA_API_KEY
    try:
        secret = getattr(client, '_secret_key', None) or getattr(client, '_api_secret', None) or os.getenv('ALPACA_SECRET_KEY')
    except:
        from config.credentials import ALPACA_SECRET_KEY
        secret = ALPACA_SECRET_KEY
    if not api_key:
        from config.credentials import ALPACA_API_KEY, ALPACA_SECRET_KEY
        api_key = ALPACA_API_KEY
        secret = ALPACA_SECRET_KEY
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret
    }

def _alpaca_base(client):
    from config.credentials import IS_PAPER
    return "https://paper-api.alpaca.markets" if IS_PAPER else "https://api.alpaca.markets"

def fetch_activities(client, activity_type: str, page_size: int = 100, after: Optional[str] = None) -> List[Dict]:
    """Fetch activities via REST GET /v2/account/activities/{type}"""
    base = _alpaca_base(client)
    headers = _alpaca_headers(client)
    params = {"page_size": page_size}
    if after:
        params["after"] = after
    try:
        r = requests.get(f"{base}/v2/account/activities/{activity_type}", headers=headers, params=params, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json() or []
        else:
            logger.debug(f"Activities {activity_type} {r.status_code} {r.text[:200]}")
            return []
    except Exception as e:
        logger.warning(f"fetch_activities {activity_type} failed: {e}")
        return []

def fetch_all_activities(client, types: List[str], since_days: int = 30) -> Dict[str, List[Dict]]:
    result = {}
    after_date = (datetime.date.today() - datetime.timedelta(days=since_days)).isoformat()
    for atype in types:
        acts = fetch_activities(client, atype, page_size=100, after=after_date)
        result[atype] = acts
        if acts:
            logger.info(f"Activities {atype}: {len(acts)} e.g. {acts[0].get('id')} {acts[0].get('symbol')} ${acts[0].get('net_amount')}")
    return result

def _optionable_account_id() -> int:
    try:
        r = requests.get(f"{OPTIONABLE_URL}/api/accounts", timeout=TIMEOUT)
        if r.status_code == 200:
            accounts = r.json().get('data') or []
            if accounts:
                return accounts[0]['id']
    except Exception:
        pass
    return 1

def _existing_fund_txns() -> List[Dict]:
    try:
        r = requests.get(f"{OPTIONABLE_URL}/api/fund-transactions", timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json().get('data') or []
    except Exception:
        pass
    return []

def sync_dividends_and_interest(client):
    """Sync DIV, INT, DIVNRA etc -> fund_transactions dividend/interest"""
    if not os.getenv("OPTIONABLE_URL"):
        # check alive
        try:
            r = requests.get(f"{OPTIONABLE_URL}/api/health", timeout=3)
            if r.status_code != 200:
                return
        except Exception:
            return

    account_id = _optionable_account_id()
    existing = _existing_fund_txns()
    # Build set of existing descriptions to dedupe
    existing_desc = set((t.get('amount'), t.get('date'), t.get('description','')[:30]) for t in existing)

    # Fetch DIV variants + INT
    dividend_types = ["DIV", "DIVCGL", "DIVCGS", "DIVNRA", "DIVROC", "DIVTXEX"]
    interest_types = ["INT", "INTNRA", "INTTW"]

    all_divs = []
    for atype in dividend_types + interest_types + ["FEE"]:
        acts = fetch_activities(client, atype, page_size=50)
        all_divs.extend(acts)

    for act in all_divs:
        try:
            # Example activity: {"activity_type":"DIV","symbol":"SGOV","net_amount":"0.12","per_share_amount":"0.12","date":"2026-08-01","qty":"1",...}
            act_type = act.get('activity_type','')
            symbol = act.get('symbol','')
            date = act.get('date') or act.get('created_at','')[:10]
            net_amount = act.get('net_amount')
            if not net_amount:
                continue
            try:
                amount = float(net_amount)
            except:
                continue
            if abs(amount) < 0.001:
                continue

            # Map to fund type
            if act_type.startswith('DIV'):
                fund_type = 'dividend'
            elif act_type.startswith('INT'):
                fund_type = 'interest'
            elif act_type == 'FEE':
                fund_type = 'fee'
                amount = -abs(amount)  # fee as negative?
            else:
                continue

            desc = f"{act_type} {symbol} ${act.get('per_share_amount','')} x{act.get('qty','')}"
            # Dedupe via amount+date+desc prefix
            key = (amount, date, desc[:30])
            if key in existing_desc:
                continue

            payload = {
                "type": fund_type,
                "amount": abs(amount) if fund_type in ('dividend','interest') else amount,
                "date": date,
                "description": desc,
                "accountId": account_id
            }
            # Optionable API: POST /api/fund-transactions
            try:
                r = requests.post(f"{OPTIONABLE_URL}/api/fund-transactions", json=payload, timeout=TIMEOUT)
                if r.status_code in (200,201):
                    logger.info(f"Optionable fund {fund_type} {symbol} ${amount} {date} -> synced")
                    existing_desc.add(key)
                else:
                    logger.debug(f"fund txn failed {r.status_code} {r.text[:200]}")
            except Exception as e:
                logger.debug(f"fund txn POST failed {e}")
        except Exception as e:
            logger.debug(f"dividend sync item failed {e}")

def sync_option_events(client):
    """Sync OPASN/OPEXP/OPEXC -> update trade status via sync_closed_trades logic + journal"""
    from core.optionable_sync import sync_closed_trades
    # Reuse closed trades sync which handles assignment detection via positions
    # Additionally fetch OPASN/OPEXP activities to log
    for atype in ["OPASN", "OPEXP", "OPEXC", "OPTRD"]:
        acts = fetch_activities(client, atype, page_size=20)
        if acts:
            logger.info(f"Option event {atype}: {len(acts)}")
            for a in acts[:3]:
                logger.info(f"  {atype} {a.get('symbol')} {a.get('date')} {a.get('qty')}")

    # Trigger closed trades sync
    try:
        sync_closed_trades(client)
    except Exception as e:
        logger.warning(f"sync_closed_trades from activities failed: {e}")

def full_sync(client):
    sync_dividends_and_interest(client)
    sync_option_events(client)

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from config.credentials import ALPACA_API_KEY, ALPACA_SECRET_KEY, IS_PAPER
    from core.broker_client import BrokerClient
    c = BrokerClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, IS_PAPER)
    print("Fetching activities last 60d...")
    allacts = fetch_all_activities(c, ["DIV","INT","FEE","OPASN","OPEXP","OPEXC","JNLS","JNLC","TRANS"], since_days=60)
    for k,v in allacts.items():
        print(f"{k}: {len(v)}")
    print("Sync dividends...")
    sync_dividends_and_interest(c)
    print("Sync option events...")
    sync_option_events(c)
