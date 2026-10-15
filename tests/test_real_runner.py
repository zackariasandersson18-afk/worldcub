from rewardbot import real_runner


class R:
    def __init__(self, body):
        self._b = body

    def json(self):
        return self._b


class HTTP:
    def __init__(self, geo):
        self.geo, self.calls = geo, []

    def get(self, url, timeout=None):
        self.calls.append(url)
        return R(self.geo)

    def request(self, *a, **k):
        self.calls.append(a)
        raise AssertionError('must not touch GitHub or trade when blocked/off')


def run(monkeypatch, tmp_path, geo, trading='on'):
    http = HTTP(geo)
    monkeypatch.setattr(real_runner.requests, 'Session', lambda: http)
    monkeypatch.setenv('REAL_TRADING', trading)
    monkeypatch.setenv('POLY_PRIVATE_KEY', 'k')
    monkeypatch.setenv('POLY_FUNDER', '0xabc')
    monkeypatch.setattr(real_runner.real_live, 'client', lambda: (_ for _ in ()).throw(AssertionError('no client')))
    return real_runner.main(['--state', str(tmp_path), '--minutes', '0']), http


def test_refuses_to_trade_from_a_blocked_connection(monkeypatch, tmp_path):
    rc, http = run(monkeypatch, tmp_path, {'blocked': True, 'country': 'US'})
    assert rc == 0 and http.calls == ['https://polymarket.com/api/geoblock']   # no orders, no next run


def test_unknown_geoblock_answer_counts_as_blocked(monkeypatch, tmp_path):
    rc, http = run(monkeypatch, tmp_path, {})
    assert rc == 0 and len(http.calls) == 1


def test_does_nothing_when_real_trading_is_off(monkeypatch, tmp_path):
    rc, http = run(monkeypatch, tmp_path, {'blocked': False, 'country': 'SE'}, trading='off')
    assert rc == 0 and len(http.calls) == 1
