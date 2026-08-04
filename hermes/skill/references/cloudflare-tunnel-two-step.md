# Cloudflare Tunnel Two-Step — DNS CNAME Often Forgotten (2026-08-03)

## Incident
Added `wheel.smitpatel.net` and `optionable.smitpatel.net` to `~/.cloudflared/config.yml` ingress and restarted tunnel, but public URL returned `ERR_NAME_NOT_RESOLVED` / 1033 until DNS CNAME created.

## Two Steps Required

### 1. Config ingress
```yaml
# ~/.cloudflared/config.yml
tunnel: b826eba9-c615-4358-8fb2-b6b0277ffbd3
credentials-file: /home/smitpatel316/.cloudflared/b826eba9-c615-4358-8fb2-b6b0277ffbd3.json
ingress:
  - hostname: wheel.smitpatel.net
    service: http://localhost:8096
  - hostname: optionable.smitpatel.net
    service: http://localhost:8098
  - service: http_status:404
```
Restart:
```bash
sudo systemctl restart cloudflared
# or
cloudflared tunnel --config ~/.cloudflared/config.yml run pi-tunnel
```

### 2. Create DNS CNAME routing to tunnel (MANDATORY, not auto)
```bash
cloudflared tunnel route dns pi-tunnel wheel.smitpatel.net
cloudflared tunnel route dns pi-tunnel optionable.smitpatel.net
# Output: INF Added CNAME <host> which will route to this tunnel tunnelID=...
```

Verify:
```bash
curl -sI https://wheel.smitpatel.net/ | head -3 # 200
curl -sI https://optionable.smitpatel.net/ | head -3 # 200 after route
# If still ERR_NAME_NOT_RESOLVED, wait 10-30s propagation
```

## Pitfalls
- Config-only change without `route dns` → tunnel runs but no DNS, browser ERR_NAME_NOT_RESOLVED. User sees 1033 error page.
- The CNAME is created in Cloudflare DNS dashboard as type CNAME pointing to <tunnelID>.cfargotunnel.com
- `market.smitpatel.net` → 8097 market-dashboard python PID 518759, keep, don't overwrite
- Existing hosts on budupi as of 2026-08-03: nba (3003), nba-backend (8001), wealth (8080), sync (8384), vault (3467), immich (2283), photos (2283), market (8097), hubble (8092), orbit (8093), quiet (8094), tame (8095), wheel (8096), optionable (8098)

## Port Mapping Reality
Always check before claiming port:
```bash
sudo lsof -i :8098
ss -tulpn | grep 8098
ps aux | grep market-dashboard
sg docker -c "docker ps --format '{{.Names}} {{.Ports}}'"
```

## Health Check
Tunnel runs as systemd service `cloudflared.service` (not Docker), PID from /usr/local/bin/cloudflared, auto-restart via cron */5 min pgrep check.

## Quick Status
```bash
ps aux | grep cloudflared | grep -v grep
systemctl status cloudflared --no-pager | tail -10
curl -s http://localhost:8098/api/health | jq  # optionable local health
curl -s http://localhost:8096/api/allocation-data | jq # wheeler local
```
