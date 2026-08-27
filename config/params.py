# Production wheel v2.5.4 - $100k paper hybrid + SPAXX sweep + RH MCP + P/L bugfix
# Unified repo at ~/wheel-stack - source of truth copied from ~/options-wheel/config/params.py + v2.5.3/2.5.4 fixes
#
# History:
# v2.4 - $100k paper hybrid with Finnhub+Alpha Vantage, earnings 503 fallback, dividend ex-dates,
#        fundamentals P/E<25 Debt/Eq<0.7, IV Rank adaptive, limit mid-price
# v2.5.1 - Liquidity volume+OI trend
# v2.5.2 - Earnings critical alert + liquidity perfect
# v2.5.3 - SGOV SPAXX/RH sweep: SGOV as interest wrapper for sitting collateral
#          Fidelity model: SPAXX core holds all cash including CSP collateral, earns ~4.5% APY, still counts as collateral.
#          RH Gold: 4.3% auto on uninvested cash same wrapper.
#          SGOV iShares 0-3M T-Bill ETF 5.22% APY wrapper for Alpaca paper limitation.
#          Alpaca paper: SGOV is stock not cash collateral, so stock BP limits sweep (ideal 1007 shares $101k vs real 454 $45k)
#          Sweep logic in sync_sgov_real(): target_ideal = total_liquid-500, target_real = min(ideal, stockBP-1000+sgov_mv)
#          BP guard updated: buying_power>=2000 and (opt_bp>=2000 or total_liq>=2000) SPAXX model sweep doesn't block wheel
# v2.5.4 - P/L bugfix + RH MCP
#          BUG FIX: Optionable sync_closed_trades set closePrice=0 always => phantom $568 vs real $52 realized.
#                   Entry is sell premium, close is buy price, profit = entry - close. close=0 implied expired worthless always.
#                   Fixed via _fetch_buy_price + get_close_price_from_activities fetching actual Alpaca BUY fill price.
#          RH MCP: https://agent.robinhood.com/mcp/trading official MCP - tools get_option_positions, place_option_order etc
#          Currently RH only long options, cannot sell CSP core wheel requirement -> stay Alpaca for now, but add flag RH_MCP_ENABLED
#          Spread/Closer hardening: SPREAD_MAX_ABS 0.15 / SPREAD_MAX_PCT 0.12 / SPREAD_NTM_MAX 0.05 Sophie non-negotiable,
#          Closer 50% profit take Reddit trader style, roller close-before-open 2s delay mandatory.
#          IS_PAPER handling: credentials.IS_PAPER bool, commission 0 paper else 0.65, affects optionable_sync _commission_for_trade()
#
# Closes remaining gaps: earnings 503 fallback, dividend ex-dates, fundamentals P/E<25 Debt/Eq<0.7, IV Rank adaptive, limit mid-price
# 90k diversified 25 tickers, roller 3% OTM, spread $0.15/12% NTM $0.05, VIX Yahoo v8 real 15.6, Alpha Vantage 131 tools

# v2.7: MAX_RISK is now the FALLBACK cap. The live cap is dynamic: total
# liquid capital deployable into CSPs without margin (cash + treasury ETF
# value - SGOV_CASH_BUFFER), scaled by market regime in context_analyzer.
MAX_RISK = 90_000
DELTA_MIN = 0.18
DELTA_MAX = 0.35  # adaptive via IV rank: 0.20 when VIX>25 or IVRank>50
YIELD_MIN = 0.008
YIELD_MAX = 0.50
EXPIRATION_MIN = 14
EXPIRATION_MAX = 60
OPEN_INTEREST_MIN = 100
SCORE_MIN = 0.02
MIN_PREMIUM = 0.20

# Spread / liquidity (Sophie non-negotiable) - v2.1 hardening + v2.5.4 verified
# NTM spread max $0.05 tight, but allow wider for ITM/high vol? Main filters:
SPREAD_MAX_ABS = 0.15  # max $0.15 absolute spread, else reject unless critical roll
SPREAD_MAX_PCT = 0.12  # max 12% of mid
SPREAD_NTM_MAX = 0.05  # near-the-money $0.05 tight for best execution v2.5.4
# Liquidity OI trend v2.5.2: volume 5d vs 20d trend penalty 0.9 if drying

# Rolling v2.1 + v2.5 assignment avoidance debit override + v2.5.4 close-before-open
ROLLING_OTM = 0.03  # 3% OTM buffer, < triggers roll (was 0.05 too sensitive)
MIN_CREDIT = 0.10   # min $0.10 credit for roll, critical override allows -$0.20 debit to avoid assignment
DTE_CRITICAL = 3    # DTE <=3 near expiry -> assignment avoidance urgency critical
DELTA_THRESHOLD = 0.50  # delta >0.50 high assignment risk
ROLL_CLOSE_BEFORE_OPEN_DELAY = 2.0  # seconds mandatory close-before-open v2.5.4 to free BP
ROLL_DEBIT_ALLOWANCE_CRITICAL = -0.20  # allow up to -$0.20 debit when DTE<=1 OTM<1% to avoid assignment (0% target per paper 371% roll rate)

# Earnings v2.3 + v2.4 503 fallback + v2.5.2 critical alert
EARNINGS_BLOCK_DAYS = 3
EARNINGS_BLOCK_DTE = 21
EARNINGS_CACHE_DAYS = 30
EARNINGS_ENABLED = True
EARNINGS_CRITICAL_DAYS = 1  # TOMORROW/TODAY explicit @mention Telegram v2.5.2
EARNINGS_503_RETAIN_HOURS = 48  # retain stale cache if Fal 503

# Dividend v2.4 - block calls if ex-div during DTE (early assignment risk), puts low risk but log
DIVIDEND_BLOCK_DAYS = 2  # Block call if ex-div within 2 days
DIVIDEND_BLOCK_DTE = True  # Block call if ex-div during DTE
DIVIDEND_CACHE_TTL = 12*3600  # 12h cache, dividends change less often
DIVIDEND_ENABLED = True

# Fundamentals v2.4 - Sophie quant filters + v2.5 BALANCE_SHEET true Debt/Eq
FUNDAMENTALS_ENABLED = True
PE_MAX = 25.0
DEBT_EQUITY_MAX = 0.7  # penalty 0.92 if >0.7, block if >1.75 extreme (AAPL 1.36 example)
DIV_YIELD_MIN = 0.0  # was 1.5% Sophie, relaxed - we hold SGOV for yield
MARKET_CAP_MIN = 1_000_000_000  # $1B min
FUNDAMENTALS_CACHE_TTL = 24*3600  # 24h, fundamentals slow-changing

# Growth screen v2.6 - block names where BOTH revenue and earnings are shrinking YoY.
# Deteriorating names are assignment traps: they drift down, get you assigned, stay assigned.
GROWTH_BLOCK_ENABLED = True

# IV Rank / Volatility v2.4 + v2.5 AD adaptive
IV_RANK_ENABLED = True
IV_RANK_LOW = 20  # below = low IV, wait or wider OTM
IV_RANK_HIGH = 50  # above = high IV, aggressive sells -> delta 0.20 tighten
IV_RANK_EXTREME = 80
# Adaptive delta based on IV/RV: VIX>25 or IVRank>50 => DELTA_MAX 0.20 aggressive cash-secured
VOLATILITY_CACHE_TTL = 4*3600

# Execution v2.4 - limit at mid-price to cut slippage 0.15% assumed + v2.5.1 SGOV limit mid
LIMIT_ORDER_ENABLED = True
LIMIT_MID_OFFSET = 0.0  # 0 = mid, -0.01 = 1c better for maker, 0.02 for taker
LIMIT_WAIT_SECONDS = 8  # wait 8s for fill, then market fallback
LIMIT_PRICE_IMPROVEMENT_MIN = 0.02  # at least 2c vs market

# Option A conservative closer 50% already in closer.py - v2.5.4 logs real P/L including fees
CLOSER_PROFIT_50 = 0.50  # main: profit >=50% and DTE>3 -> take profit Reddit trader style Sophie 50% rule
CLOSER_PROFIT_TIME = 0.40  # time-efficient: 40% profit with DTE 7-21 and $0.20+ absolute -> redeploy BP
CLOSER_PROFIT_HIGH = 0.75  # high >=75% lock gains regardless
CLOSER_PAPER_FEE = 0.0
CLOSER_LIVE_FEE_PER_CONTRACT = 0.65

# SGOV Sweep v2.5.3 - SPAXX/RH wrapper interest on sitting collateral
# Fidelity SPAXX sweep: all cash including put collateral in money market earning ~4.5%, still counts as collateral
# RH Gold 4.3% auto. SGOV iShares 0-3M T-Bill 5.22% APY wrapper for Alpaca paper where stock BP limits.
# Formula: cash + sgov_mv = total_liquid, target_ideal = total_liquid-500, max_affordable = stockBP-1000, target_real = min(ideal, affordable+sgov_mv)
# MASTER SWITCH for the whole SGOV sweep (v2.5.3). True = Alpaca paper model:
# sweep idle cash into SGOV, pre-fund next-day CSP queue with SGOV sales,
# sync SGOV to Optionable, track sgov_history. False = Robinhood/Fidelity
# model where the broker's own sweep pays interest on ALL cash including CSP
# collateral: no sweep, no pre-funding, no SGOV anywhere. Everything SGOV
# (sync_sgov_real, _prefund_queue_with_sgov, place_sgov_limit_order) hangs
# off this flag so the whole mechanism can be deleted in one pass at the
# Robinhood cutover. Set False BEFORE going live at Robinhood.
SGOV_ENABLED = True
SGOV_YIELD_APY = 0.0522  # 5.22% APY
SGOV_YIELD_MONTHLY = 0.0043  # ~0.43% monthly div
SGOV_CASH_BUFFER = 500  # keep $500 cash buffer not swept
SGOV_STOCK_BP_BUFFER = 1000  # keep $1000 stock BP buffer
SGOV_TARGET_PCT = 0.99  # sweep 99% liquid to SGOV

# RH MCP official v2.5.4
# URL: https://agent.robinhood.com/mcp/trading
# Setup: hermes mcp add robinhood --url https://agent.robinhood.com/mcp/trading
# Tools documented 2026-08-03: get_accounts, get_portfolio, get_realized_pnl, get_option_chains, get_option_instruments,
# place_option_order, etc. Current limitation: long only, cannot sell CSP yet (wheel core) -> stay Alpaca, flag for future.
RH_MCP_ENABLED = False  # set True when RH adds short puts/calls support per https://robinhood.com/us/en/agentic-trading/
RH_MCP_URL = "https://agent.robinhood.com/mcp/trading"
RH_WHEEL_SUPPORTED = False  # False until place_option_order sell_to_open verified works

# P/L Tracker v2.5.4 - real vs optionable discrepancy
# Optionable bug: closePrice=0 => profit=entry phantom $568 vs real $52
# Real P/L = sum(sell_short premiums) - sum(buy_close) - fees, from Alpaca activities
PNL_TRACKER_ENABLED = True
PNL_DISCREPANCY_THRESHOLD = 50.0  # alert if Optionable vs Alpaca real diff >$50
