import numpy as np
import pandas as pd

from cryptobot import smart_quoter as sq


def test_quotes_match_the_js_engine():
    bid, ask = sq.compute_quotes(100.0, 0.0, 0.0)
    assert abs(bid - (100 - 0.075)) < 1e-9 and abs(ask - (100 + 0.075)) < 1e-9   # min spread 0.15 %
    lb, la = sq.compute_quotes(100.0, 0.0, 1.0)                                    # long: quotes shift down
    assert lb < bid and la < ask


def bars(prices):
    n = len(prices)
    return pd.DataFrame({'t': 1_790_000_000 + np.arange(n), 'high': prices, 'low': prices, 'close': prices})


def test_mean_reverting_price_earns_the_spread_and_fees_eat_it():
    p = np.array(([100.0] * 8 + [99.85] * 8) * 200)
    free = sq.simulate(bars(p), 0.0).sum()
    paid = sq.simulate(bars(p), 0.01).sum()
    assert free > 0 and paid < free


def test_touching_the_quote_is_not_a_fill():
    p = np.array([100.0] * 40 + [100 - 0.075] * 40)
    assert sq.simulate(bars(p), 0.0).sum() == 0.0
