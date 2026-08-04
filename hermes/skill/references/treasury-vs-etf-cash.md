# Wheeler Direct Treasury vs ETF Cash — Domain Notes

## CUSIP
- Acronym: Committee on Uniform Securities Identification Procedures, 9-char unique issuance ID.
- Not ticker. Ticker `SGOV` trades forever. CUSIP `912797JX2` is one specific T-Bill that matures.
- Format: `912797JX2`: Chars 1-6 issuer (`912797`=US T-Bill, `91282C`=T-Note), 7-8 issue/maturity (`JX`), 9 check digit (`2`).
- Wheeler's field `cuspid` is typo for `CUSIP`.

## Wheeler Original Model (direct)
- User buys T-Bills via broker (Fidelity TreasuryDesk / TreasuryDirect) using real CUSIP.
- Tracked in `treasuries{cuspid PK, purchased DATE, maturity DATE, amount REAL, yield REAL, buy_price, current_value, exit_price}`.
- Collateral flow: put assigned → `amount -= strike*100`, call assigned → `amount += proceeds`. Interest synthetic rows `INT-Qx-YEAR`.
- P&L = exit_price - buy_price, or current_value - buy_price, or amount - buy_price (fallback).
- Dashboard: days remaining, leverage gauges, Bonds Held pie, interest totals.

## SGOV / USFR / BIL / SHV ETF Proxy
- SGOV = iShares 0-3 Month Treasury Bond ETF. Holds basket of 0-3m T-Bills, price ~$100 (stable $100 range, slight drift), monthly div $0.42/qtr ~$0.17/mo, yield ~ Fed Funds ~5%, 0.07% expense ratio.
- USFR = WisdomTree Floating Rate Treasury, $50.38.
- BIL = SPDR Bloomberg 1-3M T-Bill, $91.69.
- Why ETF vs direct:
  - Direct: no expense fee, yield locked to maturity, but ladder management, CUSIP hunting, roll risk.
  - ETF: 0.07% fee, variable yield, but liquid, auto-rolls, simple ticker.
- For paper trading $100k: 50k idle = floor(50000 / SGOV_price) shares → tracked as long position, but treated as treasury-equivalent in allocation.
- In Wheeler DB: `symbols(SGOV) + long_positions(SGOV, 496, 100.72)` instead of `treasuries(CUSIP)`.
- This session adopted SGOV: user explicitly said "assume we bought SGOV with extra cash" instead of direct CUSIP table.

## Why This Matters for Options-Wheel Bot
- Wheel needs cash securing puts (`MAX_RISK`). Ideally idle cash earns risk-free yield.
- Option 1 (Wheeler original): buy 4-week T-Bills direct, track by CUSIP, mature to cash, manual.
- Option 2 (our Pi bot): hold SGOV in Alpaca paper as portion of portfolio, auto-synced to Wheeler as Treasury slice. Simplifies automation, one symbol price fetch vs many CUSIPs.
- Dynamic target: `idle = 100k - putExposure - longStockNonSGOV`. Base 50k (50% rule) when no positions.
