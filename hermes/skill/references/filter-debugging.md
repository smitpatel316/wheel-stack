# Filter Debugging — Options Wheel (Updated 2026-08-03)

## Session: 2026-08-02 Sun market closed
## Session: 2026-08-03 Mon market OPEN — yield/OI blocker discovery

## Price Check Results MAX_RISK=75k

From live Alpaca StockHistoricalDataClient:

- AAPL $308.52 x100 $30852 PASS
- CSCO $116.00 x100 $11600 PASS
- INTC $90.21 x100 $9021 PASS
- AMD $476.03 x100 $47603 PASS
- BAC $61.98 x100 $6198 PASS
- WFC $86.45 x100 $8646 PASS
- F $14.67 x100 $1467 PASS
- T $23.27 x100 $2326 PASS
- VZ $46.83 x100 $4683 PASS
- SBUX $105.23 x100 $10523 PASS
- KO $87.58 x100 $8758 PASS
- PG $144.51 x100 $14451 PASS
- PFE $25.03 x100 $2503 PASS
- JNJ $256.46 x100 $25646 PASS
- XOM $155.41 x100 $15541 PASS
- CVX $196.85 x100 $19686 PASS
- HON $243.09 x100 $24310 PASS
- CAT $814.55 x100 $81456 FILTER (>75k)
- NEE $86.95 x100 $8695 PASS
- DUK $125.37 x100 $12537 PASS
- LIN $478.46 x100 $47846 PASS
- MP $41.37 x100 $4137 PASS
- DLR $188.51 x100 $18851 PASS
- PLD $144.71 x100 $14471 PASS
- SPY $746.79 x100 $74679 PASS (fails at 50k)

At 50k: SPY + CAT both filtered
At 75k: 24/25 PASS, only CAT filtered.

## 2026-08-03 Live Market Trace (OPEN)

MCP: is_open=true, equity 99997, positions SGOV 496, open orders [], watchlist 25 OK

Strategy:
- filtered_symbols by buying_power 75k: 23 PASS (price fetch)
- get_options_contracts: 4087 put contracts
- snapshots: 4087
- filter_options delta 0.18-0.30 yield 0.008-0.06 OI 500 -> 0

Debug on cheap symbols F,BAC,PFE,INTC,T (905 contracts -> 46 delta -> 1 yield -> 0 OI):

- Stage1 delta 0.18-0.30: 46
- Stage2 yield: T260911P00022500 $0.05 strike 22.5 dte 39 yield 2.03% (only 1 passes) but OI None
- Stage3 OI 500: 0

Full universe delta 0.18-0.30 distribution (sample from 10 symbols):

```
AAPL 290P 14D bid 1.79 yield 15.02% OI None
AAPL 290P 18D bid 2.48 yield 16.43% OI 12370
CSCO 103P 18D 1.61 yield 30.03% OI 278
BAC 60P 18D 0.41 yield 13.13% OI 8522
WFC 83P 18D 0.57 yield 13.19% OI 1616
F 14P 18D 0.18 yield 24.7% OI 6787
T 22.5P 18D 0.19 yield 16.22% OI 725
VZ 45.5P 18D 0.33 yield 13.93% OI 1121
SBUX 100P 18D 0.63 yield 12.1% OI 2692
```
All >0.06 max. Real wheel $0.30-$1.50 on $20-$60 = 10-40% annualized.

Lower yield tail:
- WFC 80P 39D 0.34 yield 3.88% OI None -> would pass yield but fails OI
- CSCO 101P 39D 0.63 yield 5.69% OI 1 -> passes yield but low OI

OI issue: Alpaca get_option_contracts returns open_interest=None for many newer expirations:
- 262 F+BAC contracts total, only 145 have OI non-None
- T 121 total, T260911P22.5 OI=None
- Same for AAPL 14D, WFC 39D

So filter fails on both yield and OI.

## Yield Formula

```python
(bid_price / strike) * (365 / (dte + 1))
```
$0.50/$50 30D -> 11.7%, $0.40/$100 30D -> 4.7%, $0.05/$22.5 39D -> 2.03%

Original YIELD_MIN 0.04 filtered too much ($0.30-$0.80 premiums), changed to 0.008 correct for min.
But YIELD_MAX 0.06 now too low — need 0.50-1.00.

## OI Handling

`models/contract.py` uses `contract.open_interest` from Trading API GetOptionContracts, not snapshot. Falls None when Alpaca hasn't populated OI for new weeklies.

Fix options:
1. Allow `oi is None` to pass (log warning)
2. Lower OI_MIN to 100
3. Fallback: try `get_option_chain` snapshot which may have different OI source, or use volume

## Score Formula

`score = (1 - |Δ|) * (250/(DTE+5)) * (bid/strike)` - best per underlying, filter SCORE_MIN after.

## Recommendation

Raise YIELD_MAX 0.06 -> 0.50, OI 500 -> allow None or 100, re-test Monday.

## Debug Commands

```bash
source .venv/bin/activate
python - << 'PY'
from config.credentials import *
from core.broker_client import BrokerClient
from models.contract import Contract
from config.params import *
c=BrokerClient(ALPACA_API_KEY,ALPACA_SECRET_KEY,IS_PAPER)
# check yield distribution
PY
cat logs/strategy_log.json | python3 -m json.tool | tail
```
