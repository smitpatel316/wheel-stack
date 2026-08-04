# Gateway Restart Safety Guard — Pitfall 2026-08-02

## Symptom
`hermes gateway restart` via `terminal()` tool inside Telegram agent session returns:
```
exit -1, 1 lines output
[terminal] ... Gateway restart blocked from inside (safety)
```
or log id #30719 "Gateway restart blocked from inside". Same for `cronjob create` triggering restart: "Cron also blocked for safety".

Occurs because gateway process is parent of agent's tool session; restarting would kill own session (loop guard).

## Reproduction
1. Edit `~/.hermes/config.yaml` to add `mcp_servers.alpaca` with `uvx alpaca-mcp-server`
2. From Telegram chat, call `terminal(command="hermes gateway restart")`
3. Gets exit -1 safety block, Main PID unchanged, MCP tools not yet injected (tool_search for mcp__alpaca__* empty)

## Fix
Must restart via SSH outside gateway:
```bash
systemctl --user restart hermes-gateway.service
# or
~/.hermes/hermes-agent/venv/bin/hermes gateway restart   # from SSH shell, not agent
```
After restart:
- `systemctl --user status hermes-gateway.service` shows new Main PID (e.g., 2633546 at 21:58:01 PDT)
- `ps aux | grep mcp_stdio_watchdog` shows child: `/.../mcp_stdio_watchdog.py --ppid <gateway-pid> -- /home/.../uvx alpaca-mcp-server`
- `~/.hermes/hermes-agent/venv/bin/hermes mcp list` → alpaca ✓ enabled
- `~/.hermes/hermes-agent/venv/bin/hermes mcp test alpaca` → ✓ Connected 2149ms 62 tools
- Inside new agent session: `tool_search` query "alpaca" → 66 matches mcp__alpaca__*, live calls: get_account_info $100k equity $350k BP $75k options level3, get_clock is_open false next Mon 09:30 ET

## Lesson for Skill
Always document SSH restart step as mandatory after config.yaml mcp_servers edit. Inside-agent restart will fail. Include verification checklist: Main PID changed, watchdog process present, mcp list ✓, mcp test ✓, tool_search live.

Related: references/mcp-alpaca-integration.md, references/agentic-migration-2026-08-02-mcp-everywhere.md
