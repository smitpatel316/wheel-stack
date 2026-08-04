# Reddit NVDA Wheel Case Study — July vs June 2026

Source: r/Optionswheel posts 1um6myw June (24k prem 6k margin trailing S&P) and July (27k prem 6.6k margin beating S&P) — manually transcribed as Reddit blocked scraping (prove-humanity).

## Profile
- 8k shares NVDA cost $0.08 (2002 $1,100), 90% portfolio, Schwab, OptionWheelTracker, $1.5M operating capital
- Strategy: .1-.2 delta calls, occasional ATM puts happy to own, DTE 1-21 days

## June 2026 (Trailing S&P)
- $24k premiums - $6k margin = $18k net realized, unrealized -$154k on underlying (NVDA 224→200), didn't sell shares, bag holding + selling calls
- 76 trades, 94% success, 98.5% puts win, 70% calls win, avg win 3d loser 9d
- Defensive purchases May → 5,500 margined NVDA to avoid assignment of long-term low-basis shares
- Mental accounting: premiums = real cash tangible, held shares intangible not loss if not sold (author admits irrational)

## July 2026 (Beating S&P, just)
- $27k - $6.6k = $20.4k net +13% vs June, trades 74, 94% success stable, puts 89% (down from 98.5%) calls 96% (up from 70%), win 3.2d loser 5d (cut losers 44% faster)
- Strategy shift: started selling puts on other stocks after FOMO not buying AMD/SanDisk/Intel before rallies — remedy = sell puts at price happy to own
- Closed SNDK, Intel puts early before -50% chip bloodbath → saved
- Assigned 300 AMD @ $505 → dropped $440 (-13%) → recovered 2 days + calls paying gap
- Still 5k margined NVDA (down 500), calls never ITM, made money waiting to be called away
- Direct broker connection in OptionWheelTracker enabled → more accurate numbers, discrepancies vs prior months
- Trim plan $250 and $300 (8k @ $250 = $2M)
- Profits → ETFs, living expenses, other stocks

## Comparison
- Net +13%, avg premium/trade $315→$364 +15%, puts win down due to diversification into higher vol, calls win up due to NVDA stabilization
- Same overconcentration risk: $24 swing on 8k shares = $192k unrealized vs $18k realized premium — premiums feel like income but don't hedge concentration. That's why trailing then barely beating.
- Margin drag 25% of premiums: 5k margined shares = $6-6.6k interest

## Lessons for Our $100k Paper Wheel
- DTE 1-21 vs our 14-60: his 94% win from short DTE high theta. Our 14-60 safer but lower win probability. Avg hold 3d vs our 18d today.
- Early profit taker critical: closed SNDK/INTC before crash analogous to rolling engine in hybrid paper (371% roll rate)
- Diversification via CSPs at "happy to own" price = remedy for FOMO, exactly our 25-ticker universe intent
- SGOV proxy avoids margin interest drag (we have 828 shares now vs his 5k margined paying 6.6k)
- Trim via OTM CCs not market sells to preserve low basis — same as our CC phase after puts
- Success rate calculation: our scoring should track win rate puts vs calls separately, avg holding period winners vs losers like his cheat sheet

## Data Captured
- Trade counts: July 74, June 76, May 95, Apr 42, Mar 124
- Win rates: July 89% puts 96% calls 94% overall, June 98.5% puts 70% calls 94%, May 83%, Apr 76%, Mar 93%
- Holding: July winners 3.2d losers 5d, June 3d vs 9d
