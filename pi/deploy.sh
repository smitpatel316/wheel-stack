#!/bin/bash
# Wheel Stack Pi Deployment Script
# Pi budupi - unified repo for options wheel + optionable + hermes agentic

set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "=== Wheel Stack Deploy ==="
echo "Root: $ROOT_DIR"
echo "Date: $(date)"
echo ""

# 1. Env check
echo "[1/5] Checking .env..."
if [ ! -f "$ROOT_DIR/.env" ]; then
  if [ -f "$ROOT_DIR/config/.env.example" ]; then
    echo "No .env found, creating from example - EDIT REQUIRED"
    cp "$ROOT_DIR/config/.env.example" "$ROOT_DIR/.env"
    echo "Please edit $ROOT_DIR/.env with real keys then re-run"
    exit 1
  else
    echo "No .env.example found in config/, checking ~/options-wheel/.env fallback"
    if [ -f ~/options-wheel/.env ]; then
      cp ~/options-wheel/.env "$ROOT_DIR/.env"
      echo "Copied from ~/options-wheel/.env"
    fi
  fi
fi
source "$ROOT_DIR/.env" 2>/dev/null || true
echo "IS_PAPER=${IS_PAPER:-true} ALPACA_API_KEY set=${ALPACA_API_KEY:+yes}"

# 2. Docker compose up optionable
echo ""
echo "[2/5] Starting Optionable container..."
cd "$ROOT_DIR"
sg docker -c "docker compose up -d optionable" || docker compose up -d optionable
echo "Waiting for health..."
sleep 5
curl -s http://localhost:8096/api/health | python3 -m json.tool || echo "Optionable health check failed, try: curl http://localhost:8096/api/health"

# 3. Hermes cron verification
echo ""
echo "[3/5] Hermes cron jobs..."
if command -v hermes &>/dev/null; then
  hermes cronjob list 2>&1 | head -n 30 || ~/.local/bin/hermes cronjob list 2>&1 | head -n 30
else
  echo "hermes CLI not in PATH, checking ~/.local/bin/hermes"
  ls ~/.hermes/cron/jobs.json 2>&1 | head
fi

# Expected 2 jobs: tamelabs (paused) + options-wheel-agentic
# Create/update if missing
if [ -f "$ROOT_DIR/hermes/cron/options-wheel-agentic.prompt.md" ]; then
  echo "Agentic prompt exists at hermes/cron/options-wheel-agentic.prompt.md ($(wc -l < "$ROOT_DIR/hermes/cron/options-wheel-agentic.prompt.md") lines)"
  echo "To install/update cron:"
  echo "  cd $ROOT_DIR && hermes cronjob create --schedule \"5 7,10,12 * * 1-5\" --name options-wheel-agentic --skills options-wheel-trading,alpaca-mcp --prompt \"\$(cat hermes/cron/options-wheel-agentic.prompt.md)\""
fi

# 4. Cloudflare tunnel check
echo ""
echo "[4/5] Cloudflare tunnel..."
cat ~/.cloudflared/config.yml 2>&1 | grep -A2 -B2 "wheel\|optionable\|8096" | head -n 50
echo ""
echo "Ensure DNS CNAME exists:"
echo "  cloudflared tunnel route dns pi-tunnel wheel.smitpatel.net"
echo "  cloudflared tunnel route dns pi-tunnel optionable.smitpatel.net"
echo "Check tunnel: sudo systemctl status cloudflared --no-pager | tail -20"

# 5. Verification
echo ""
echo "[5/5] Verification..."
echo "Optionable: https://wheel.smitpatel.net (or http://localhost:8096)"
curl -s http://localhost:8096/api/health | python3 -c "import sys,json; print(json.load(sys.stdin))" 2>&1 || echo "health failed"
echo ""
sg docker -c "docker ps --format '{{.Names}} {{.Status}} {{.Ports}}' | grep -E 'optionable|wheel-stack'" || docker ps | grep optionable
echo ""
echo "Logs:"
echo "  tail -f ~/wheel-stack/logs/*.log or ~/options-wheel/logs/*.log"
echo "  tail -f /home/smitpatel316/optionable-data/*.log"
echo ""
echo "P/L Reconciliation (fixes $568 bug):"
echo "  cd $ROOT_DIR && python3 -c \"from core.pnl_tracker import reconcile_optionable_vs_alpaca; from config.credentials import *; from core.broker_client import BrokerClient; c=BrokerClient(ALPACA_API_KEY,ALPACA_SECRET_KEY,IS_PAPER); print(reconcile_optionable_vs_alpaca(c))\""
echo ""
echo "=== Deploy Done ==="
echo "Next: Resume tamelabs? You paused it earlier. Run: hermes cronjob resume 42ba0564c225 or leave paused."
