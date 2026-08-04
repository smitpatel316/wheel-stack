# Optionable Evaluation — Wheel Tracking Alternative (2026-08-03)

## Repo
https://github.com/yomikoye/optionable — MIT, 14 stars, 11 forks, v0.16.0, 43 commits, Docker multi-arch `yomikoye/optionable:latest` 642MB, stack React18+Vite+Tailwind+Recharts+Express+better-sqlite3 WAL, Node20 (warns requires >=22 for yahoo-finance2 but works).

## Philosophy
Fully local self-hosted, SQLite WAL, no brokerage, optional Yahoo Finance live prices, offline capable.

## Features Wheeler Lacks
- Trade chains CSP→stock→CC grouping + roll tracking via parentTradeId
- Portfolio Mode: Fund Journal (deposits/withdrawals/dividends/interest/fees), RoR, Monthly stacked P/L by source, Income donut
- Multi-account (Default + custom, filter all views)
- Commission per account ($0.66 default), dark mode, N/S/H/Esc
- CSV multi-section + duplicate detection
- Buy-side CALL/PUT live prices batch endpoint

## Gap
Tracking-only — no broker execution. Our ~/options-wheel does Alpaca Paper real market orders, ET cron, OCC parser, MAX_RISK, SGOV real buy.

## Deploy Recipe (Pi budupi)
```bash
mkdir -p ~/optionable-data
# 8097 occupied by market-dashboard python PID 518759 -> use 8098
docker compose -f ~/optionable-data/docker-compose.optionable.yml up -d
# Add ingress then DNS CNAME (mandatory)
# ~/.cloudflared/config.yml: - hostname: optionable.smitpatel.net service: http://localhost:8098
cloudflared tunnel route dns pi-tunnel optionable.smitpatel.net
```

## Port Reality 2026-08-03
- 8096 Wheeler wheel.smitpatel.net
- 8097 market-dashboard python market.smitpatel.net (do NOT kill)
- 8098 Optionable optionable.smitpatel.net

## Live A/B 2026-08-03 03:46 UTC
- Wheeler: Treasuries 49957.12 (SGOV|496|100.72)
- Optionable: tradeCount 6, Total P/L $2610
- Alpaca: 1 open BUY 496 SGOV ACCEPTED
