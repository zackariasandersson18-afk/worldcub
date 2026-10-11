from memebot import paper


class FakeHTTP:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, params=None, timeout=None, headers=None):
        for key, body in self.routes.items():
            if key in url and (callable(body) or True):
                b = body(url, params) if callable(body) else body
                return type('R', (), {'status_code': 200 if b is not None else 404, 'json': lambda self: b})()
        return type('R', (), {'status_code': 404, 'json': lambda self: None})()


def pool(addr, mint, created, liq=50000, price=1.0, buys=60, sells=20, buyers=30, vol=40000, chg=20):
    return {'attributes': {'address': addr, 'name': mint + ' / SOL', 'pool_created_at': created, 'reserve_in_usd': str(liq),
                           'base_token_price_usd': str(price), 'transactions': {'m5': {'buys': buys, 'sells': sells, 'buyers': buyers}},
                           'volume_usd': {'h1': str(vol)}, 'price_change_percentage': {'h1': str(chg)}},
            'relationships': {'base_token': {'data': {'id': 'solana_' + mint}},
                              'quote_token': {'data': {'id': 'solana_So11111111111111111111111111111111111111112'}}}}


SAFE = {'risks': [], 'token': {}, 'transferFee': {'pct': 0}, 'markets': [{'pubkey': 'P', 'lp': {'lpLockedPct': 100}}],
        'topHolders': [{'owner': 'P', 'pct': 80}, {'owner': 'w1', 'pct': 5}], 'knownAccounts': {}}


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


def test_buy_take_half_then_trailing_exit(tmp_path):
    t0 = 1_791_000_000
    created = '2026-10-04T00:00:00Z'
    import datetime as dt
    created = dt.datetime.fromtimestamp(t0 - 3600, dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    price = {'v': 1.0}
    routes = {'trending_pools': {'data': [pool('A', 'm1', created)]}, 'new_pools': {'data': []},
              'rugcheck': SAFE, 'dexscreener': lambda u, p: {'pairs': [{'pairAddress': 'A', 'priceUsd': str(price['v']), 'liquidity': {'usd': 50000}}]}}
    h = FakeHTTP(routes)
    paper.step(tmp_path, now=t0, http=h, log=lambda *_: None)
    st = paper.load(tmp_path / 'memebot_state.json', {})
    assert len(st['open']) == 1 and st['cash'] == 4750
    price['v'] = 2.2                                         # > 2 x entry (1.02): half sold
    paper.step(tmp_path, now=t0 + 60, http=h, log=lambda *_: None)
    st = paper.load(tmp_path / 'memebot_state.json', {})
    assert st['open'][0]['half_sold'] and abs(st['cash'] - (4750 + 125 * 2.2 / 1.02 * 0.98)) < 1e-6
    price['v'] = 1.0                                         # < half of the 2.2 peak: rest sold
    paper.step(tmp_path, now=t0 + 120, http=h, log=lambda *_: None)
    st = paper.load(tmp_path / 'memebot_state.json', {})
    assert not st['open'] and st['closed'][0]['reason'].startswith('trailing') and st['closed'][0]['won']


def test_rug_after_three_missing_quotes(tmp_path):
    t0 = 1_791_000_000
    import datetime as dt
    created = dt.datetime.fromtimestamp(t0 - 3600, dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    routes = {'trending_pools': {'data': [pool('A', 'm1', created)]}, 'new_pools': {'data': []}, 'rugcheck': SAFE,
              'dexscreener': {'pairs': []}}
    h = FakeHTTP(routes)
    for i in range(4):
        paper.step(tmp_path, now=t0 + 60 * i, http=h, log=lambda *_: None)
    st = paper.load(tmp_path / 'memebot_state.json', {})
    assert st['closed'][0]['reason'] == 'rug / pair gone' and st['closed'][0]['pnl'] == -250


def test_manual_close(tmp_path):
    t0 = 1_791_000_000
    import datetime as dt
    created = dt.datetime.fromtimestamp(t0 - 3600, dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    routes = {'trending_pools': {'data': [pool('A', 'm1', created)]}, 'new_pools': {'data': []}, 'rugcheck': SAFE,
              'dexscreener': {'pairs': [{'pairAddress': 'A', 'priceUsd': '1.5', 'liquidity': {'usd': 50000}}]}}
    h = FakeHTTP(routes)
    paper.step(tmp_path, now=t0, http=h, log=lambda *_: None)
    paper.step(tmp_path, now=t0 + 60, http=h, log=lambda *_: None, close={'A'})
    st = paper.load(tmp_path / 'memebot_state.json', {})
    c = st['closed'][0]
    assert c['manual'] and c['reason'] == 'manuellt stängd' and abs(c['realised'] - 250 * 1.5 / 1.02 * 0.98) < 1e-6


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
