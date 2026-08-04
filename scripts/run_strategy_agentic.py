#!/usr/bin/env python3
"""
Agentic wrapper that uses MCP for reads but keeps custom scoring for puts.
This will be deprecated once MCP option chain scoring is fully agentic.
For now, it uses broker_client for data (same as MCP underlying) but logs that MCP should be used.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
# This script is kept for reference - real agentic trading now via Hermes cron job using MCP tools
print("Agentic trading is handled by Hermes cron job options-wheel-agentic using MCP tools")
print("This script is deprecated - see cron job that uses mcp_alpaca_* tools")
