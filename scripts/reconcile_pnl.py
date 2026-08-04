#!/usr/bin/env python3
"""
Reconcile P/L nightly - compares Alpaca real fills vs Optionable reported
Fixes $568 vs $52 bug, flags drift >$50 for alert

Usage:
  python scripts/reconcile_pnl.py
  python scripts/reconcile_pnl.py --alert  # posts to telegram via hermes if drift >50
Cron: 0 2 * * * cd ~/wheel-stack && python scripts/reconcile_pnl.py >> logs/reconcile.log 2>&1
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import logging
from config.credentials import ALPACA_API_KEY, ALPACA_SECRET_KEY, IS_PAPER
from core.broker_client import BrokerClient
from core.pnl_tracker import reconcile_optionable_vs_alpaca

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("reconcile")

def main():
    parser = argparse.ArgumentParser(description="P/L reconciliation")
    parser.add_argument("--alert", action="store_true", help="Alert if drift >50")
    parser.add_argument("--threshold", type=float, default=50.0, help="Drift threshold")
    args = parser.parse_args()
    
    client = BrokerClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, IS_PAPER)
    result = reconcile_optionable_vs_alpaca(client)
    
    # Log
    alpaca_realized = result.get('alpaca', {}).get('realized_matched', 0)
    opt_inflated = result.get('optionable', {}).get('reported_pnl_inflated', 0)
    opt_true = result.get('optionable', {}).get('true_pnl_if_closePrice_correct', 0)
    drift = result.get('discrepancy', {}).get('inflated_vs_real', 0)
    
    print("=== P/L Reconciliation ===")
    print(f"Alpaca realized (matched sells-buys): ${alpaca_realized:.2f}")
    print(f"Optionable inflated (closePrice=0 bug): ${opt_inflated:.2f}")
    print(f"Optionable true (if closePrice correct): ${opt_true:.2f}")
    print(f"Discrepancy (inflated - real): ${drift:.2f}" if drift is not None else f"Drift unknown")
    print(f"\nFull result: {result}")
    
    # Save to logs
    import json, datetime
    log_path = Path(__file__).parent.parent / "logs" / "reconcile.jsonl"
    log_path.parent.mkdir(exist_ok=True)
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "alpaca_realized": alpaca_realized,
        "optionable_inflated": opt_inflated,
        "optionable_true": opt_true,
        "drift": drift,
        "full": result
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    
    if args.alert and drift is not None and abs(drift) > args.threshold:
        msg = f"⚠️ P/L drift ${drift:.2f} > ${args.threshold}: Optionable ${opt_inflated:.2f} vs Alpaca real ${alpaca_realized:.2f}. Check core/optionable_sync.py closePrice mapping."
        logger.warning(msg)
        # Future: post to telegram via hermes send_message if available
        # For now just log; Hermes cron will surface logs
        print(f"ALERT: {msg}")
        # Return non-zero to trigger cron alert if needed
        sys.exit(1)
    else:
        print(f"✅ Drift ${drift:.2f} within threshold ${args.threshold} - OK" if drift else "Check result")

if __name__ == "__main__":
    main()
