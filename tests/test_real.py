import json

from rewardbot import real


def book(bids, asks):
    return {'bids': [{'price': str(p), 'size': str(z)} for p, z in bids], 'asks': [{'price': str(p), 'size': str(z)} for p, z in asks]}


C = {'cond': '0xc1', 'question': 'Q1', 'yes': 'Y1', 'no': 'N1', 'rate': 40.0, 'v': 6.5, 'min_size': 20.0, 'd': 0.0325, 'collateral': 18.7}


def test_quote_rounds_away_from_mid_and_prices_both_bids():
    yb, nb = book([(0.38, 50)], [(0.72, 50)]), book([(0.28, 50)], [(0.62, 50)])   # mid 0.55
    q = real.quote(C, yb, nb, 0.01)
    assert q['yes_bid'] == 0.51 and q['no_bid'] == 0.41                          # 0.5175 -> 0.51, 0.4175 -> 0.41
    assert q['share'] == 1.0 and q['est_day'] == 40.0 and q['cost'] == round(20 * 0.92, 2)


def test_quote_skips_outside_band_or_when_rounding_leaves_the_paid_spread():
    assert real.quote(C, book([(0.02, 50)], [(0.10, 50)]), {}, 0.01) is None     # mid 0.06
    assert real.quote({**C, 'v': 1.0, 'd': 0.005}, book([(0.40, 50)], [(0.70, 50)]), {}, 0.01) is None   # 0.545 -> 0.54 = 1 c away


def test_plan_fits_the_capital_and_counts_changes(tmp_path, monkeypatch):
    c2 = {**C, 'cond': '0xc2', 'yes': 'Y2', 'no': 'N2', 'rate': 30.0}
    (tmp_path / 'rewards_v4_state.json').write_text(json.dumps({'portfolio': [C, c2],
                                                                'positions': {'0xc1': {'share': 1.0}, '0xc2': {'share': 1.0}}}))
    books = {'Y1': book([(0.38, 50)], [(0.72, 50)]), 'N1': book([(0.28, 50)], [(0.62, 50)]),
             'Y2': book([(0.38, 50)], [(0.72, 50)]), 'N2': book([(0.28, 50)], [(0.62, 50)])}
    monkeypatch.setattr(real.bt, 'fetch_books', lambda toks, http: books)
    monkeypatch.setattr(real.bt, '_get', lambda http, url, params=None: {'minimum_tick_size': 0.01})
    monkeypatch.setattr(real, 'CAPITAL', 20.0)                                   # room for one market (18.40)
    p = real.maybe_plan(str(tmp_path), None, now=1000, log=lambda *_: None)
    assert [m['cond'] for m in p['markets']] == ['0xc1'] and len(p['orders']) == 2 and p['changes'] is None
    assert real.maybe_plan(str(tmp_path), None, now=1300, log=lambda *_: None) is None      # every 10 min
    books['Y1'] = book([(0.40, 50)], [(0.74, 50)])                                          # mid moves to 0.57
    p = real.maybe_plan(str(tmp_path), None, now=1700, log=lambda *_: None)
    assert p['changes'] == {'new': 2, 'cancelled': 2}
