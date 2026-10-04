from arbbot.scan import Basket, Leg, best_fill, baskets_from_event, fee


def legs(asks_per_leg, rate=0.0):
    return [Leg(str(i), f't{i}', rate, 1.0, a) for i, a in enumerate(asks_per_leg)]


def test_all_yes_arbitrage_walks_the_books_until_it_stops_paying():
    b = Basket('e', 's', 'ALL-YES', 1.0, legs([[(0.30, 10), (0.40, 50)], [(0.30, 20)], [(0.30, 100)]]))
    r = best_fill(b)
    # 10 baskets at 0.90, then the first leg costs 0.40 -> 1.00, no longer profitable
    assert abs(r['baskets'] - 10) < 1e-9 and abs(r['profit'] - 1.0) < 1e-9 and r['executable']


def test_fees_can_remove_the_arbitrage():
    asks = [[(0.33, 100)], [(0.33, 100)], [(0.33, 100)]]
    assert best_fill(Basket('e', 's', 'ALL-YES', 1.0, legs(asks)))['profit'] > 0
    assert 3 * (0.33 + fee(0.33, 0.05, 1)) > 1
    assert best_fill(Basket('e', 's', 'ALL-YES', 1.0, legs(asks, rate=0.05)))['baskets'] == 0


def test_budget_caps_the_fill():
    b = Basket('e', 's', 'ALL-NO', 2.0, legs([[(0.60, 1000)], [(0.60, 1000)], [(0.60, 1000)]]))
    r = best_fill(b, budget=18.0)   # 1.80 per basket -> 10 baskets
    assert abs(r['baskets'] - 10) < 1e-9 and abs(r['profit'] - 2.0) < 1e-9


def test_missing_book_or_closed_outcome_means_no_basket():
    assert best_fill(Basket('e', 's', 'ALL-YES', 1.0, legs([[(0.2, 10)], []])))['baskets'] == 0
    ev = {'title': 't', 'slug': 's', 'markets': [
        {'clobTokenIds': '["a","b"]'}, {'clobTokenIds': '["c","d"]', 'closed': True}]}
    assert baskets_from_event(ev, True) == []


def test_all_yes_only_for_exhaustive_events():
    ev = {'title': 't', 'slug': 's', 'markets': [{'clobTokenIds': '["a","b"]'}, {'clobTokenIds': '["c","d"]'}]}
    assert [b.kind for b in baskets_from_event(ev, False)] == ['ALL-NO']
    kinds = {b.kind: b for b in baskets_from_event(ev, True)}
    assert kinds['ALL-NO'].payout == 1 and kinds['ALL-YES'].payout == 1
    assert [lg.token for lg in kinds['ALL-NO'].legs] == ['b', 'd']


def test_cross_platform_pricing_counts_both_legs_and_fees():
    from types import SimpleNamespace as N
    from arbbot.cross_kalshi import price_pair, theirs
    pm = N(yes_bid=0.40, yes_ask=0.42, fee_rate=0.0, fee_exp=1.0)
    km = N(yes_bid=0.50, yes_ask=0.52, no_bid=0.48, no_ask=0.50)
    r = price_pair(pm, km)
    # YES on Polymarket 0.42 + NO on Kalshi 0.50 = 0.92, Kalshi fee 0.07*0.25 = 0.0175
    assert r['direction'].startswith('YES@Polymarket') and abs(r['edge'] - (1 - 0.92 - 0.0175)) < 1e-9
    edge, signal = theirs(pm, km)
    assert signal and edge > 0
    same = N(yes_bid=0.49, yes_ask=0.51, no_bid=0.49, no_ask=0.51)
    assert price_pair(N(yes_bid=0.49, yes_ask=0.51, fee_rate=0.0, fee_exp=1.0), same)['edge'] < 0
