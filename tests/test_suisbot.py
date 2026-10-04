import numpy as np
import pandas as pd
import pytest

from suisbot import btc5m


def test_rsi_matches_wilder_extremes():
    assert btc5m.rsi(list(range(1, 40))) == 100.0
    assert btc5m.rsi([1.0] * 10) == 50.0          # too short
    down = list(range(40, 1, -1))
    assert btc5m.rsi(down) == pytest.approx(0.0, abs=1e-9)


def test_signal_reproduces_their_rules():
    flat = {'rsi': 50, 'm1': 0, 'm5': 0, 'm15': 0, 'vwap_dev': 0, 'sma': 0}
    s = btc5m.signal(flat, 0.50)
    assert s['p_up'] == pytest.approx(0.50) and not s['ok']          # no convergence
    strong_up = {'rsi': 40, 'm1': 0.2, 'm5': 0.2, 'm15': 0.2, 'vwap_dev': 0.1, 'sma': 0.05}
    s = btc5m.signal(strong_up, 0.50)
    assert s['side'] == 'UP' and s['p_up'] <= 0.65 and s['ok']
    assert btc5m.signal(strong_up, 0.60)['ok'] is False                # entry above 0.55


def test_kelly_size_caps():
    assert btc5m.kelly_size(0.65, 0.5) == 75.0                         # $75 hard cap
    assert btc5m.kelly_size(0.45, 0.5) == 0.0


def windows_and_candles(n=40, seed=1):
    rng = np.random.default_rng(seed)
    t0 = 1_790_000_000 // 300 * 300
    mins = np.arange(t0 - 2 * 3600, t0 + n * 300 + 3600, 60)
    close = 60000 * np.exp(np.cumsum(rng.normal(0, 0.001, len(mins))))
    candles = pd.DataFrame({'open': close, 'high': close * 1.0005, 'low': close * 0.9995,
                            'close': close, 'volume': 1.0}, index=mins)
    ws = []
    for k in range(n):
        start = t0 + k * 300
        w = btc5m.Window(f'btc-updown-5m-{start}', start, start + 300, bool(rng.random() < 0.5), f't{k}', 0.07, 1.0)
        # a later price must never be used
        ws.append((w, [(start + 300 - 1800 - 60, 0.50), (start + 300 - 1800 + 60, 0.99)]))
    return ws, candles


def test_backtest_uses_only_past_prices_and_charges_fees():
    ws, candles = windows_and_candles()
    res = btc5m.run(ws, candles)
    for t in res['trades']:
        assert t['price'] == 0.50                                       # not the later 0.99
        assert t['cost_real'] == pytest.approx(0.50 + 0.005 + 0.07 * 0.25)
        assert t['pnl_real'] <= t['pnl_theirs']


def test_parse_window_reads_resolution_and_fee():
    e = {'slug': 'btc-updown-5m-1791103200', 'startTime': '2026-10-04T08:40:00Z',
         'markets': [{'outcomePrices': '["1", "0"]', 'closed': True, 'endDate': '2026-10-04T08:45:00Z',
                      'eventStartTime': '2026-10-04T08:40:00Z', 'clobTokenIds': '["a", "b"]',
                      'feeSchedule': {'rate': 0.07, 'exponent': 1}}]}
    w = btc5m.parse_window(e)
    assert w.up_won and w.end - w.start == 300 and w.fee_rate == 0.07 and w.up_token == 'a'
