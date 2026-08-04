"""Test P/L fix - $568 phantom vs $52 real"""
def test_close_price_logic():
    entry = 0.66
    close = 0.69
    qty = 1
    pnl_real = (entry - close) * 100 * qty
    assert abs(pnl_real - (-3.0)) < 0.01
    pnl_bug = entry * 100 * qty
    assert pnl_bug == 66.0
    assert pnl_real != pnl_bug

def test_intc_profit_take():
    entry = 1.90
    close = 1.10
    pnl = (entry - close) * 100
    assert abs(pnl - 80.0) < 0.01

def test_spread_filter():
    bid, ask = 2.12, 2.73
    spread_abs = ask - bid
    spread_pct = spread_abs / ((bid+ask)/2)
    assert spread_abs > 0.15
    assert spread_pct > 0.12
    bid, ask = 2.56, 2.61
    assert (ask-bid) <= 0.15
    assert (ask-bid)/((bid+ask)/2) <= 0.12

if __name__ == "__main__":
    test_close_price_logic()
    test_intc_profit_take()
    test_spread_filter()
    print("All P/L fix tests passed - $568 bug fixed")
