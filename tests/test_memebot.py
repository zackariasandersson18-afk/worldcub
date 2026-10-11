from memebot import paper


class FakeHTTP:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, params=None, timeout=None, headers=None):
        for key, body in self.routes.items():
            if key in url and (callable(body) or True):
                b = body(url, params) if callable(body) else body
                code, b = b if isinstance(b, tuple) else (200 if b is not None else 404, b)
                return type('R', (), {'status_code': code, 'json': lambda self: b})()
        return type('R', (), {'status_code': 404, 'json': lambda self: None})()


def pool(addr, mint, created, liq=50000, price=1.0, buys=60, sells=20, buyers=30, vol=40000, chg=20):
    return {'attributes': {'address': addr, 'name': mint + ' / SOL', 'pool_created_at': created, 'reserve_in_usd': str(liq),
                           'base_token_price_usd': str(price), 'transactions': {'m5': {'buys': buys, 'sells': sells, 'buyers': buyers}},
                           'volume_usd': {'h1': str(vol)}, 'price_change_percentage': {'h1': str(chg)}},
            'relationships': {'base_token': {'data': {'id': 'solana_' + mint}},
                              'quote_token': {'data': {'id': 'solana_So11111111111111111111111111111111111111112'}}}}


SAFE = {'risks': [], 'token': {'decimals': 6}, 'transferFee': {'pct': 0}, 'markets': [{'pubkey': 'P', 'lp': {'lpLockedPct': 100}}],
        'topHolders': [{'owner': 'P', 'pct': 80}, {'owner': 'w1', 'pct': 5}], 'knownAccounts': {}}


USDC = paper.USDC


def jupiter(price, impact=0.01, route=lambda: True):
    """Fake Jupiter at `price` USD per token (6 decimals both sides) with `impact` lost on every swap."""
    def f(url, params):
        if not route():
            return 400, {'error': 'no route', 'errorCode': 'NO_ROUTES_FOUND'}
        a = int(params['amount'])
        out = a / price['v'] if params['inputMint'] == USDC else a * price['v']
        return {'outAmount': str(int(out * (1 - impact))), 'priceImpactPct': str(impact)}
    return f


def sell_kr(tokens_raw, px, impact=0.01):
    return tokens_raw * px * (1 - impact) / 1e6 * (1 - paper.LATENCY) * paper.KR_PER_USD - paper.FEE_KR


def test_filters():
    c = {'quote': 'So11111111111111111111111111111111111111112', 'age_s': 3600, 'liq': 30000, 'price': 1,
         'm5': {'buys': 30, 'sells': 10, 'buyers': 25}, 'vol_h1': 20000, 'chg_h1': 10}
    assert paper.market_ok(c) is None
    assert paper.market_ok({**c, 'liq': 10000}) == 'liquidity'
    assert paper.market_ok({**c, 'chg_h1': 300}) == 'price change'
    assert paper.safety(SAFE) is None                                        # the pool's 80 % is not a holder
    assert paper.safety({**SAFE, 'risks': [{'level': 'danger'}]}) == 'danger risk'
    assert paper.safety({**SAFE, 'topHolders': [{'owner': 'w', 'pct': 40}]}) == 'concentrated holders'
    assert paper.safety({**SAFE, 'markets': [{'pubkey': 'P', 'lp': {'lpLockedPct': 10}}]}) == 'lp not locked'


def created_at(t0):
    import datetime as dt
    return dt.datetime.fromtimestamp(t0 - 3600, dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def routes_for(price, t0, **jup):
    return {'trending_pools': {'data': [pool('A', 'm1', created_at(t0))]}, 'new_pools': {'data': []}, 'rugcheck': SAFE,
            'jup.ag': jupiter(price, **jup),
            'dexscreener': lambda u, p: {'pairs': [{'pairAddress': 'A', 'priceUsd': str(price['v']), 'liquidity': {'usd': 50000}}]}}


def test_buy_take_half_then_trailing_exit_at_jupiter_prices(tmp_path):
    t0, price = 1_791_000_000, {'v': 1.0}
    h = FakeHTTP(routes_for(price, t0))
    paper.step(tmp_path, now=t0, http=h, log=lambda *_: None)
    st = paper.load(tmp_path / 'memebot_state.json', {})
    p = st['open'][0]
    tokens = int(int(24.8e6 * 0.99) * (1 - paper.LATENCY))       # 248 kr = 24.8 USD after the 2 kr fee, 1 % impact, 0.5 % latency
    assert st['cash'] == 4750 and p['tokens'] == tokens and abs(p['entry'] - 25 / (tokens / 1e6)) < 1e-12
    assert 0.022 < p['entry_cost'] < 0.025                         # fee + impact + latency, about 2.3 %
    price['v'] = 2.2                                               # > 2 x the real entry: half sold at the quote
    paper.step(tmp_path, now=t0 + 60, http=h, log=lambda *_: None)
    st = paper.load(tmp_path / 'memebot_state.json', {})
    p = st['open'][0]
    assert p['half_sold'] and p['tokens_left'] == tokens - tokens // 2
    assert abs(st['cash'] - (4750 + sell_kr(tokens // 2, 2.2))) < 1e-4
    assert abs(p['quote_kr'] - sell_kr(p['tokens_left'], 2.2)) < 1e-3
    price['v'] = 1.0                                               # < half of the 2.2 peak: rest sold
    paper.step(tmp_path, now=t0 + 120, http=h, log=lambda *_: None)
    st = paper.load(tmp_path / 'memebot_state.json', {})
    c = st['closed'][0]
    assert not st['open'] and c['reason'].startswith('trailing') and c['won']
    assert abs(c['realised'] - sell_kr(tokens // 2, 2.2) - sell_kr(tokens - tokens // 2, 1.0)) < 1e-4


def test_no_jupiter_route_skips_the_buy(tmp_path):
    t0 = 1_791_000_000
    h = FakeHTTP(routes_for({'v': 1.0}, t0, route=lambda: False))
    paper.step(tmp_path, now=t0, http=h, log=lambda *_: None)
    st = paper.load(tmp_path / 'memebot_state.json', {})
    assert not st['open'] and st['skips'] == {'no route': 1} and st['cash'] == 5000


def test_unsellable_after_three_minutes_without_route(tmp_path):
    t0, price, ok = 1_791_000_000, {'v': 1.0}, {'v': True}
    h = FakeHTTP(routes_for(price, t0, route=lambda: ok['v']))
    paper.step(tmp_path, now=t0, http=h, log=lambda *_: None)
    ok['v'], price['v'] = False, 0.5                               # stop hit, but nobody buys
    for i in (1, 2):
        paper.step(tmp_path, now=t0 + 60 * i, http=h, log=lambda *_: None)
        st = paper.load(tmp_path / 'memebot_state.json', {})
        assert st['open'][0]['exit_wait'] == 'stop -40 %' and not st['closed']
    paper.step(tmp_path, now=t0 + 180, http=h, log=lambda *_: None)
    st = paper.load(tmp_path / 'memebot_state.json', {})
    assert st['closed'][0]['reason'].startswith('ingen säljväg') and st['closed'][0]['pnl'] == -250


def test_rug_after_three_missing_quotes(tmp_path):
    t0 = 1_791_000_000
    r = routes_for({'v': 1.0}, t0)
    r['dexscreener'] = {'pairs': []}
    h = FakeHTTP(r)
    for i in range(4):
        paper.step(tmp_path, now=t0 + 60 * i, http=h, log=lambda *_: None)
    st = paper.load(tmp_path / 'memebot_state.json', {})
    assert st['closed'][0]['reason'] == 'rug / pair gone' and st['closed'][0]['pnl'] == -250


def test_manual_close(tmp_path):
    t0, price = 1_791_000_000, {'v': 1.0}
    h = FakeHTTP(routes_for(price, t0))
    paper.step(tmp_path, now=t0, http=h, log=lambda *_: None)
    price['v'] = 1.5
    paper.step(tmp_path, now=t0 + 60, http=h, log=lambda *_: None, close={'A'})
    st = paper.load(tmp_path / 'memebot_state.json', {})
    c = st['closed'][0]
    assert c['manual'] and c['reason'] == 'manuellt stängd' and abs(c['realised'] - sell_kr(c['tokens'], 1.5)) < 1e-4


def test_price_trail_for_chart():
    p = {'trail': []}
    for i in range(paper.TRAIL_MAX + 5):
        paper.track(p, 1000 + 60 * i, 1.234567891)
    assert len(p['trail']) == paper.TRAIL_MAX and p['trail'][0] == [1000 + 60 * 5, 1.23457]
    st = {'cash': 0, 'open': [p], 'closed': []}
    p.update(stake=250, stake_left=250, entry=1.0, realised=0.0)
    paper.exit_all(st, p, 1.0, 'x', 'iso')
    assert len(p['trail']) <= 300 and p['trail'][-1][0] == 1000 + 60 * (paper.TRAIL_MAX + 4)


def test_close_requests_only_from_owner(monkeypatch):
    from rewardbot import loop
    monkeypatch.setenv('GITHUB_REPOSITORY', 'me/repo')
    monkeypatch.setenv('GITHUB_TOKEN', 't')
    issues = [{'number': 1, 'title': 'memebot: stäng POOL1', 'user': {'login': 'me'}},
              {'number': 2, 'title': 'memebot: stäng POOL2', 'user': {'login': 'stranger'}},
              {'number': 3, 'title': 'something else', 'user': {'login': 'me'}},
              {'number': 4, 'title': 'memebot: stäng POOL4', 'user': {'login': 'me'}, 'pull_request': {}}]
    h = type('H', (), {'get': lambda self, *a, **k: type('R', (), {'status_code': 200, 'json': lambda s: issues})()})()
    assert loop.close_requests(h) == [{'number': 1, 'pool': 'POOL1'}]


def test_jupiter_silent_falls_back_to_estimate(tmp_path):
    t0, price, up = 1_791_000_000, {'v': 1.0}, {'v': True}
    r = routes_for(price, t0)
    j = r['jup.ag']
    r['jup.ag'] = lambda u, p: j(u, p) if up['v'] else (502, None)
    h = FakeHTTP(r)
    paper.step(tmp_path, now=t0, http=h, log=lambda *_: None)
    up['v'], price['v'] = False, 0.5
    paper.step(tmp_path, now=t0 + 60, http=h, log=lambda *_: None)
    c = paper.load(tmp_path / 'memebot_state.json', {})['closed'][0]
    assert c['reason'] == 'stop -40 %' and c['estimated'] and c['pnl'] < -100


def run(tmp_path, h, t, book='66', **kw):
    paper.step(tmp_path, now=t, http=h, log=lambda *_: None, book=book, **kw)
    return paper.load(tmp_path / paper.BOOKS[book]['file'], {})


def test_variant_a_time_stop_after_two_hours_without_progress(tmp_path):
    t0, price = 1_791_000_000, {'v': 1.0}
    h = FakeHTTP(routes_for(price, t0))
    run(tmp_path, h, t0, 'A')
    price['v'] = 1.2                                               # up, but peak < 1.3 x entry
    assert run(tmp_path, h, t0 + 3600, 'A')['open']
    st = run(tmp_path, h, t0 + 2 * 3600, 'A')
    assert not st['open'] and st['closed'][0]['reason'] == 'tidsstopp 2 h'
    assert run(tmp_path, h, t0, 'K')['open'] and run(tmp_path, h, t0 + 2 * 3600, 'K')['open']   # control keeps it


def test_variant_b_sells_when_liquidity_flees(tmp_path):
    t0, price, liq = 1_791_000_000, {'v': 1.0}, {'v': 50000}
    r = routes_for(price, t0)
    r['dexscreener'] = lambda u, p: {'pairs': [{'pairAddress': 'A', 'priceUsd': str(price['v']), 'liquidity': {'usd': liq['v']}}]}
    h = FakeHTTP(r)
    run(tmp_path, h, t0, 'B')
    liq['v'] = 36000                                               # 72 % of the 50 000 at entry: hold
    assert run(tmp_path, h, t0 + 60, 'B')['open']
    liq['v'] = 34000                                               # 68 %: out
    st = run(tmp_path, h, t0 + 120, 'B')
    assert not st['open'] and st['closed'][0]['reason'] == 'likviditetsflykt'


def test_variant_c_sells_in_steps(tmp_path):
    t0, price = 1_791_000_000, {'v': 1.0}
    h = FakeHTTP(routes_for(price, t0))
    st = run(tmp_path, h, t0, 'C')
    tokens = st['open'][0]['tokens']
    price['v'] = 2.2
    p = run(tmp_path, h, t0 + 60, 'C')['open'][0]
    assert p['tranches'] == 1 and p['tokens_left'] == tokens - int(tokens / 3)
    price['v'] = 4.5
    p = run(tmp_path, h, t0 + 120, 'C')['open'][0]
    left = tokens - int(tokens / 3)
    assert p['tranches'] == 2 and p['tokens_left'] == left - int(left * 0.5)
    price['v'] = 6.0                                               # 5x would end the base rules, not C
    assert run(tmp_path, h, t0 + 180, 'C')['open']
    price['v'] = 2.9                                               # < half the 6.0 peak
    st = run(tmp_path, h, t0 + 240, 'C')
    assert st['closed'][0]['reason'].startswith('trailing') and st['closed'][0]['won']


def test_step_all_shares_one_market_and_fills_the_shadow_book(tmp_path):
    t0, price, calls = 1_791_000_000, {'v': 1.0}, []
    r = routes_for(price, t0)
    r['rugcheck'] = lambda u, p: calls.append('rc') or SAFE
    r['trending_pools'] = lambda u, p: calls.append('gt') or {'data': [pool('A', 'm1', created_at(t0)),
                                                                       pool('Z', 'm9', created_at(t0), buys=10, sells=30)]}
    h = FakeHTTP(r)
    paper.step_all(tmp_path, now=t0, http=h, log=lambda *_: None)
    assert calls.count('gt') == 2 and calls.count('rc') == 2       # 2 trending pages, 2 mints: once each, not per book
    for b in paper.BOOKS.values():
        assert len(paper.load(tmp_path / b['file'], {})['open']) == 1
    sh = paper.load(tmp_path / 'shadow.json', {})
    assert {x['pool']: x['fails'] for x in sh['live']} == {'A': [], 'Z': ['buy pressure']}
    price['v'] = 1.4
    paper.step_all(tmp_path, now=t0 + 1800, http=h, log=lambda *_: None)
    x = paper.load(tmp_path / 'shadow.json', {})['live'][0]
    assert x['path'][-1] == [30, 1.4, 50]
    paper.step_all(tmp_path, now=t0 + 48 * 3600, http=h, log=lambda *_: None)
    assert not paper.load(tmp_path / 'shadow.json', {})['live'] or all(y['t0'] > t0 for y in paper.load(tmp_path / 'shadow.json', {})['live'])
    assert len(list((tmp_path / 'shadow').glob('*.json'))) == 1


def test_live_file_has_open_and_recently_closed_without_trails(tmp_path):
    t0, price = 1_791_000_000, {'v': 1.0}
    h = FakeHTTP(routes_for(price, t0))
    paper.step_all(tmp_path, now=t0, http=h, log=lambda *_: None)
    price['v'] = 0.5                                               # every book hits its stop
    paper.step_all(tmp_path, now=t0 + 60, http=h, log=lambda *_: None)
    live = paper.load(tmp_path / paper.LIVE_FILE, {})
    assert set(live['books']) == set(paper.BOOKS)
    b = live['books']['66']
    assert not b['open'] and b['closed_recent'][0]['reason'] == 'stop -40 %' and 'trail' not in b['closed_recent'][0]
    paper.step_all(tmp_path, now=t0 + 60 + paper.LIVE_CLOSED_S + 60, http=h, log=lambda *_: None)
    assert not paper.load(tmp_path / paper.LIVE_FILE, {})['books']['66']['closed_recent']
