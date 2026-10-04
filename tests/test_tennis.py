from datetime import date

from tennisbot.hold_favourite import decide, evaluate, parse_market

START = 1_790_000_000


def mk(winner=0, fee=0.0):
    return {'slug': 'atp-a-b-2026-09-01', 'start': START, 'outcomes': ['A', 'B'], 'token0': 't',
            'winner': winner, 'fee_rate': fee, 'fee_exp': 1.0}


def test_buys_the_favourite_from_prices_before_the_start_only():
    hist = [(START - 7200, 0.40), (START - 600, 0.35), (START + 600, 0.95)]   # in-play price ignored
    d = decide(mk(winner=1), hist)
    assert d['favourite'] == 'B' and abs(d['price'] - 0.65) < 1e-9 and d['won']
    assert decide(mk(), [(START - 8 * 3600, 0.7)]) is None                     # too stale
    assert decide(mk(), [(START + 60, 0.7)]) is None


def test_fees_and_spread_only_hurt():
    d = decide(mk(winner=0, fee=0.02), [(START - 60, 0.70)])
    assert d['pnl_theirs'] > d['pnl_real'] > 0
    assert decide(mk(winner=1), [(START - 60, 0.70)])['pnl_real'] == -10.0


def test_parse_market_takes_singles_moneyline_with_a_decisive_resolution():
    base = {'slug': 'atp-a-b-2026-09-01', 'markets': [
        {'sportsMarketType': 'games', 'outcomes': '["Over","Under"]'},
        {'sportsMarketType': 'moneyline', 'outcomes': '["A","B"]', 'outcomePrices': '["0","1"]',
         'clobTokenIds': '["x","y"]', 'gameStartTime': '2026-09-01 12:00:00+00'}]}
    m = parse_market(base)
    assert m['winner'] == 1 and m['token0'] == 'x'
    assert parse_market({**base, 'slug': 'atp-doubles-a-b-2026-09-01'}) is None
    base['markets'][1]['outcomePrices'] = '["0.5","0.5"]'
    assert parse_market(base) is None


def test_evaluate_has_gates():
    ts = []
    for i in range(30):
        d = decide(mk(winner=i % 3 == 0), [(START - 60, 0.70)])
        d['day'] = date(2026, 9, 1 + i % 28)
        ts.append(d)
    assert 'gates' in evaluate(ts, 30)
