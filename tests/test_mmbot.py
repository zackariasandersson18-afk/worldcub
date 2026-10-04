from datetime import date

from mmbot import backtest as mm
from mmbot.backtest import Market, quotes, reference, simulate

H = 3600


def mk(trades, yes_won=False, end_h=50, key='m'):
    return Market(key, date(2026, 9, 1), 'X', '20°C', end_h * H, yes_won, sorted(trades))


def test_quotes_round_away_from_reference_and_stay_in_range():
    assert quotes(0.50, 0.02) == (0.48, 0.52)
    assert quotes(0.503, 0.02) == (0.48, 0.53)
    assert quotes(0.03, 0.02) == (None, None) and quotes(None, 0.02) == (None, None)


def test_reference_uses_only_trades_strictly_before_t():
    tr = [(10, 0.4, 1), (H, 0.6, 1)]
    assert reference(tr, H, 0)[0] == 0.4          # the trade at exactly t is not yet known
    assert reference(tr, H + 1, 0)[0] == 0.6
    assert reference(tr, 10 + mm.REF_MAX_AGE + 1, 0)[0] is None or reference(tr, 10 + mm.REF_MAX_AGE + 1, 0)[0] == 0.6


def test_future_trades_do_not_change_earlier_quotes_or_fills():
    base = [(0, 0.50, 5), (H + 10, 0.45, 20), (2 * H + 10, 0.55, 20)]
    a = simulate([mk(base + [(30 * H, 0.10, 100)])], 0.02)['rows'][0]
    b = simulate([mk(base + [(30 * H, 0.90, 100)])], 0.02)['rows'][0]
    # fills in hours 1-2 are identical; only what happens later differs
    assert a['fills'] >= 2 and b['fills'] >= 2


def test_round_trip_earns_the_spread_when_price_mean_reverts():
    tr = [(0, 0.50, 5)]
    for k in range(1, 21):
        tr.append((k * H + 10, 0.45 if k % 2 else 0.55, 20))   # through the bid, then through the ask
        tr.append((k * H + 20, 0.50, 1))                       # reference back to 0.50
    row = simulate([mk(tr, end_h=30)], 0.02)['rows'][0]
    assert row['fills'] >= 20 and row['pnl'] > 0
    assert abs(row['pnl'] - row['spread']) < 1e-6              # flat inventory: no adverse selection


def test_adverse_selection_loses_when_price_trends_to_zero():
    tr = [(0, 0.60, 5)] + [(k * H + 10, round(0.60 - 0.03 * k, 2), 50) for k in range(1, 17)]
    row = simulate([mk(tr, yes_won=False, end_h=20)], 0.02)['rows'][0]
    assert row['pnl'] < 0 and row['spread'] > 0


def test_trade_exactly_at_our_price_is_not_a_fill():
    tr = [(0, 0.50, 5), (H + 10, 0.48, 50), (H + 20, 0.52, 50)]
    assert simulate([mk(tr, end_h=5)], 0.02)['rows'][0]['fills'] == 0


def test_short_then_cover_nets_ask_minus_bid():
    # hour 1: trade above ask 0.52 -> we sell 10 YES short; hour 2 (ref 0.50): below bid 0.48 -> buy back
    tr = [(0, 0.50, 5), (H + 10, 0.53, 10), (H + 20, 0.50, 1), (2 * H + 10, 0.47, 10), (2 * H + 20, 0.50, 1)]
    row = simulate([mk(tr, yes_won=True, end_h=4)], 0.02)['rows'][0]
    assert abs(row['pnl'] - 10 * (0.52 - 0.48)) < 1e-9


def test_capital_limits_orders():
    tr = [(0, 0.50, 5)] + [(k * H + 10, 0.40, 1000) for k in range(1, 10)]
    sim = simulate([mk(tr, end_h=12)], 0.02, capital=10.0)
    assert sim['min_wallet'] >= -1e-9
