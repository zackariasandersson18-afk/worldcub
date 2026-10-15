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


def test_compute_leaves_out_bids_for_tokens_already_held(tmp_path, monkeypatch):
    (tmp_path / 'rewards_v4_state.json').write_text(json.dumps({'portfolio': [C], 'positions': {'0xc1': {'share': 1.0}}}))
    monkeypatch.setattr(real.bt, 'fetch_books', lambda toks, http: {'Y1': book([(0.38, 50)], [(0.72, 50)]),
                                                                     'N1': book([(0.28, 50)], [(0.62, 50)])})
    c = real.compute(str(tmp_path), None, 50.0, {'0xc1': 0.01}, held={'Y1': 20.0})
    assert [o['outcome'] for o in c['orders']] == ['NO'] and len(c['markets']) == 1


def _two_markets(tmp_path, monkeypatch, books):
    crowded = {**C, 'cond': 'crowded', 'yes': 'Yc', 'no': 'Nc', 'rate': 100.0}
    empty = {**C, 'cond': 'empty', 'yes': 'Ye', 'no': 'Ne', 'rate': 20.0}
    (tmp_path / 'rewards_v4_state.json').write_text(json.dumps({'portfolio': [crowded, empty],
                                                                'positions': {'crowded': {'share': 1.0}, 'empty': {'share': 0.1}}}))
    monkeypatch.setattr(real.bt, 'fetch_books', lambda toks, http: books)


def test_picks_by_live_share_not_by_v4s_old_estimate(tmp_path, monkeypatch):
    # someone rests 5000 shares 1 cent either side of the 0.55 mid
    books = {'Yc': book([(0.38, 50), (0.54, 5000)], [(0.72, 50), (0.56, 5000)]), 'Nc': book([(0.28, 50)], [(0.62, 50)]),
             'Ye': book([(0.38, 50)], [(0.72, 50)]), 'Ne': book([(0.28, 50)], [(0.62, 50)])}
    _two_markets(tmp_path, monkeypatch, books)
    c = real.compute(str(tmp_path), None, 20.0, {'crowded': 0.01, 'empty': 0.01})
    assert [m['cond'] for m in c['markets']] == ['empty'] and c['alternatives'][0]['cond'] == 'crowded'
    assert c['markets'][0]['share'] == 1.0 and c['alternatives'][0]['share'] < 0.05


def test_own_orders_are_not_competition_and_keep_a_head_start(tmp_path, monkeypatch):
    books = {'Yc': book([(0.38, 50), (0.53, 20)], [(0.72, 50)]), 'Nc': book([(0.28, 50), (0.43, 20)], [(0.62, 50)]),
             'Ye': book([(0.38, 50)], [(0.72, 50)]), 'Ne': book([(0.28, 50)], [(0.62, 50)])}
    _two_markets(tmp_path, monkeypatch, books)
    own = {'Yc': [(0.53, 20)], 'Nc': [(0.43, 20)]}             # those 20-share bids at 0.53 / 0.43 are ours
    c = real.compute(str(tmp_path), None, 20.0, {'crowded': 0.01, 'empty': 0.01}, own=own)
    assert [m['cond'] for m in c['markets']] == ['crowded'] and c['markets'][0]['share'] == 1.0
    assert [(b['price'], float(b['size'])) for b in real.without_own(books['Yc'], own['Yc'])['bids']] == [('0.38', 50.0)]
