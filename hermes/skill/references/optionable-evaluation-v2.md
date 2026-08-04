# Optionable Evaluation vs Wheeler — 2026-08-03

Stack: React18+Vite+Tailwind+Recharts+Express+better-sqlite3 WAL, Docker multi-arch yomikoye/optionable:latest 642MB, Node20 warns >=22 needed for yahoo-finance2 but works, SQLite local, offline capable, MIT.

Directory structure:
server/
  index.js createApp+startServer
  db/connection.js singleton WAL pragmas, migrations.js 14 versions, seed.js demo data
  middleware CORS JSON parser security headers
  routes health/trades CRUD+roll+import, stats recursive CTE, positions, prices single+batch, settings, accounts, fundTransactions, stocks, portfolio stats+monthly
  utils conversions.js toCents/toDollars tradeToApi etc, response.js, validation.js
src/
  App.jsx tab routing, layout Header TabBar, dashboard KPI 6 metrics, chart PnLChart, trades TradeTable chain grouping TradeModal, positions PositionsTable, portfolio PortfolioView Dashboard MonthlyPLChart IncomeSourcesChart FundJournal StocksTable, settings, hooks useTrades useStats useAccounts usePortfolio, services api.js

Tables INTEGER cents:
- trades ticker type CSP/CC/CALL/PUT strike qty delta entryPrice closePrice openedDate expirationDate closedDate status Open/Expired/Assigned/Closed/Rolled parentTradeId notes commission accountId
- positions ticker shares costBasis acquiredDate acquiredFromTradeId soldDate salePrice capitalGainLoss accountId
- accounts name commissionPerContract
- fund_transactions accountId type deposit/withdrawal/dividend/interest/fee amount date description
- stocks accountId ticker shares costBasis acquiredDate soldDate salePrice notes
- price_cache ticker price change changePercent
- settings live_prices_enabled portfolio_mode_enabled etc

API list GET/POST/PUT/DELETE /api/trades, /trades/:id, /trades/import, /trades/roll atomic close+create, /stats, /positions, /positions/summary, /prices/:ticker, /prices/batch, /accounts CRUD, /fund-transactions, /stocks, /portfolio/stats, /portfolio/monthly.

Features Wheeler lacks:
- Trade chains grouped, roll tracking, parentTradeId chain
- Portfolio Mode toggle Settings -> Options/Portfolio Tabs: Portfolio Dashboard KPI Deposited Total P/L RoR Options P/L Stock Gains Income, Fund Journal deposits/withdrawals/dividends/interest/fees full CRUD, Monthly P/L stacked bar by source, Income donut, StocksTable ticker aggregation expandable lots, manual stock buy/sell P/L, context-aware header New Trade vs Buy Stock
- Multi-account selector header All Accounts filter, CRUD
- Commission per-account $/contract auto calc legs 1 for Open/Expired/Assigned 2 for Closed/Rolled
- Dark mode, keyboard N new trade S settings H help Esc close, Welcome guide
- Live prices Yahoo yahoo-finance2 single + batch caching 1h TTL, option chain batch POST /api/prices/options/batch grouped ticker+expiry, price column green/red per type
- CSV multi-section format trades+fund+stocks backward compat, duplicate detection skip + toast
- Pagination configurable 5/10/25/50/Show All, persistent trades_per_page setting

Gap: tracking-only no brokerage execution. Our options-wheel does Alpaca Paper market_sell for puts/calls + SGOV real buy 496 via MarketOrderRequest + ET cron.

Deploy verified Pi:
8097 occupied market-dashboard py PID 518759 -> must use 8098 then 8096
Tunnel: ingress + route dns both needed or ERR_NAME_NOT_RESOLVED
Health: /api/health tradeCount 6 seed example NVDA CSP Assigned 130 $3.8 etc, stats totalPnL 1810 premium 2460 winRate 100
Live: optionable.smitpatel.net Dashboard Options P/L $1230 5 trades AVG ROI 2.05% WIN 100% 3 chains STOCK GAINS $1380 TOTAL $2610 DEPLOYED $56k chart, Trade Log 4 chains table.
Recommendation: keep execution in options-wheel, use Optionable dashboard via core/optionable_sync.py POST /api/trades adapter.
