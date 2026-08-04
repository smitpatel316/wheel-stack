# Cron Setup — Options Wheel ET Sessions

## Goal

Run at 10:05am, 1:05pm, 3:35pm ET weekdays. Avoid 9:30 open volatility, buffer before 4pm close.

Budupi runs PDT (UTC-7 summer). EDT is UTC-4.

- 10:05 ET = 14:05 UTC = 07:05 PDT
- 13:05 ET = 17:05 UTC = 10:05 PDT
- 15:35 ET = 19:35 UTC = 12:35 PDT

In EST winter (UTC-5): times shift by 1 hour — cron in PDT will be 1hr off. For precise ET, use wrapper that checks NY time or use UTC cron and convert to UTC fixed.

Simplest prod: use PDT times as above and accept 1hr drift in winter, or use UTC cron via `TZ=America/New_York` in crontab if system supports CRON_TZ.

## Wrapper Script

`~/options-wheel/run_wheel_cron.sh`:

```bash
#!/bin/bash
set -e
cd /home/smitpatel316/options-wheel
source .venv/bin/activate
run-strategy --strat-log --log-level INFO --log-to-file 2>&1 | tee -a logs/cron.log
```

Must `chmod +x` and must source venv — `run-strategy` is in `.venv/bin/`.

Absolute path to repo not relative, because cron CWD is HOME.

## Crontab

Clean install (avoid duplicate append bug seen 2026-08-02):

```bash
cat > /tmp/clean_cron <<'CRON'
# Cloudflare Tunnel - auto-restart if dead
*/5 * * * * pgrep -f "cloudflared tunnel" > /dev/null || /usr/local/bin/cloudflared tunnel run --credentials-file /home/smitpatel316/.cloudflared/b826eba9-c615-4358-8fb2-b6b0277ffbd3.json pi-tunnel >> /home/smitpatel316/.cloudflared/pi-tunnel.log 2>&1
# Options Wheel - paper trading 10:05am,1:05pm,3:35pm ET (7:05,10:05,12:35 PDT M-F)
5 7 * * 1-5 /home/smitpatel316/options-wheel/run_wheel_cron.sh >> /home/smitpatel316/options-wheel/logs/cron.log 2>&1
5 10 * * 1-5 /home/smitpatel316/options-wheel/run_wheel_cron.sh >> /home/smitpatel316/options-wheel/logs/cron.log 2>&1
35 12 * * 1-5 /home/smitpatel316/options-wheel/run_wheel_cron.sh >> /home/smitpatel316/options-wheel/logs/cron.log 2>&1
CRON
crontab /tmp/clean_cron
crontab -l
```

Previous bug: `crontab -l > old; echo ... >> old; crontab old` without dedup produced duplicate cloudflared + duplicate wheel entries after re-run. Always rewrite full file via `/tmp/clean_cron` template.

## Logs

- `logs/run.log` — from `setup_logger(to_file=True)`
- `logs/strategy_log.json` — structured trades, allowed/filtered symbols, puts/calls
- `logs/cron.log` — wrapper tee + cron stdout
- `~/.cloudflared/pi-tunnel.log` — tunnel health

Check with `cat logs/cron.log | tail -100`

## Verify After Install

```bash
source .venv/bin/activate
run-strategy --strat-log --log-level INFO --log-to-file
python3 -m json.tool logs/strategy_log.json | tail -80
```

Market closed handling: cron will run on weekend if pattern is `1-5` it won't, but if market holiday Monday market closed → filtered put_options empty expected, not error. Don't alert on that.

## Pi Systemd Alternative

Could also run via systemd timer with `OnCalendar=Mon..Fri 07:05,10:05,12:35` and `Environment=TZ=America/New_York` but cron is simpler for Pi.

## Safety

Do not run with `--fresh-start` in cron — that liquidates all positions. Cron should be idempotent normal mode.
