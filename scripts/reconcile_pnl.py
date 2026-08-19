#!/usr/bin/env python3
"""
Reconcile P/L nightly - compares Alpaca real fills vs Optionable reported
Canonical: ~/wheel-stack

Usage:
  python scripts/reconcile_pnl.py
  python scripts/reconcile_pnl.py --alert
Cron: 0 2 * * * cd ~/wheel-stack && python scripts/reconcile_pnl.py >> logs/reconcile.log 2>&1
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import argparse
import logging
import json
import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("reconcile")

def main():
    parser = argparse.ArgumentParser(description="P/L reconciliation")
    parser.add_argument("--alert", action="store_true")
    parser.add_argument("--threshold", type=float, default=50.0)
    args = parser.parse_args()

    from config.credentials import ALPACA_API_KEY, ALPACA_SECRET_KEY, IS_PAPER
    from core.broker_client import BrokerClient

    client = BrokerClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, IS_PAPER)

    try:
        from core.pnl_tracker import get_pnl_summary_for_logging
        result = get_pnl_summary_for_logging(client)
        print("=== P/L Reconciliation (v2.6.0 unified) ===")
        print(json.dumps(result, indent=2, default=str))

        drift = result.get("discrepancy", 0)
        if isinstance(drift, dict):
            drift = drift.get("inflated_vs_real", 0) or drift.get("value", 0) or 0
        try:
            drift_val = float(drift or 0)
        except Exception as e:
            logger.warning("[SWALLOWED] drift value %r not float-parseable, treating as 0.0: %r", drift, e)
            drift_val = 0.0

        log_path = ROOT / "logs" / "reconcile.jsonl"
        log_path.parent.mkdir(exist_ok=True)
        entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "result": result,
            "drift": drift_val,
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

        if args.alert and abs(drift_val) > args.threshold:
            print(f"ALERT: P/L drift ${drift_val:.2f} > ${args.threshold}")
            sys.exit(1)
        else:
            print(f"OK Drift ${drift_val:.2f} within threshold ${args.threshold}")
            return

    except Exception as e:
        logger.warning(f"get_pnl_summary_for_logging failed {e}, fallback")
        import traceback
        traceback.print_exc()

    try:
        from core.pnl_tracker import reconcile_optionable_vs_alpaca
        result = reconcile_optionable_vs_alpaca(client)
        print("=== Fallback reconcile ===")
        print(json.dumps(result, indent=2, default=str))
    except Exception as e2:
        logger.warning("[SWALLOWED] fallback reconcile_optionable_vs_alpaca failed: %r", e2)
        print(f"Reconciliation failed: {e2}")
        import traceback
        traceback.print_exc()
        sys.exit(2)

if __name__ == "__main__":
    main()
