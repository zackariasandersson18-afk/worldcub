"""Real-money module against a fake Polymarket client: nothing here talks to Polymarket."""
import json

import pytest

from rewardbot import real_live


class FakeClient:
    def __init__(self, cash=40.0):
        self.cash, self.orders, self.posted, self.cancelled, self.cancel_all_calls, self.n = cash, [], [], [], 0, 0

    def get_balance_allowance(self, params):
        return {'balance': str(int(self.cash * 1e6))}

    def get_orders(self, params=None):
        return list(self.orders)

    def cancel_orders(self, ids):
        self.cancelled += ids
        self.orders = [o for o in self.orders if o['id'] not in ids]

    def cancel_all(self):
        self.cancel_all_calls += 1
        self.orders = []

    def create_order(self, args):
        return args

    def post_order(self, signed, order_type, post_only=False):
        assert post_only, 'every order must be post-only'
        self.n += 1
        self.posted.append((signed.token_id, signed.price, signed.size))
        self.orders.append({'id': f'o{self.n}', 'asset_id': signed.token_id, 'price': str(signed.price),
                            'original_size': str(signed.size), 'size_matched': '0', 'status': 'LIVE'})
        return {'success': True}

    def are_orders_scoring(self, params):
        return {i: True for i in params.orderIds}


class Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


class FakeHTTP:
    def __init__(self, issues=(), positions=()):
        self.issues, self.positions, self.answers = list(issues), list(positions), []

    def get(self, url, params=None, headers=None, timeout=None):
        if 'api.github.com' in url:
            return Resp(200, self.issues)
        return Resp(200, self.positions)

    def post(self, url, json=None, headers=None, timeout=None):
        self.answers.append(json['body'])

    def patch(self, url, json=None, headers=None, timeout=None):
        self.issues = []


PLAN = {'used': 36.8, 'est_reward_day': 100.0, 'markets': [{'q': 'Q'}],
        'orders': [{'cond': 'c1', 'token': 'Y1', 'outcome': 'YES', 'side': 'BUY', 'price': 0.28, 'size': 20.0},
                   {'cond': 'c1', 'token': 'N1', 'outcome': 'NO', 'side': 'BUY', 'price': 0.64, 'size': 20.0}]}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv('REAL_TRADING', 'on')
    monkeypatch.setenv('POLY_PRIVATE_KEY', 'not-a-real-key')
    monkeypatch.setenv('POLY_FUNDER', '0xabc')
    monkeypatch.setenv('GITHUB_REPOSITORY', 'owner/repo')
    monkeypatch.setenv('GITHUB_TOKEN', 't')
    monkeypatch.setattr(real_live.real, 'compute', lambda state, http, cap, ticks, held=None: json.loads(json.dumps(PLAN)))


def test_off_unless_key_and_switch(monkeypatch):
    monkeypatch.delenv('REAL_TRADING', raising=False)
    monkeypatch.setenv('POLY_PRIVATE_KEY', 'k')
    monkeypatch.setenv('POLY_FUNDER', '0xabc')
    assert not real_live.enabled()
    monkeypatch.setenv('REAL_TRADING', 'on')
    assert real_live.enabled()
    monkeypatch.delenv('POLY_PRIVATE_KEY')
    assert not real_live.enabled()


def test_posts_missing_orders_once_and_reports_scoring(tmp_path, on):
    cli, http = FakeClient(), FakeHTTP()
    row = real_live.tick(str(tmp_path), http, now=1000, log=lambda *_: None, cli=cli)
    assert sorted(cli.posted) == [('N1', 0.64, 20.0), ('Y1', 0.28, 20.0)] and row['orders'] == 2 and row['scoring'] == 2
    real_live.tick(str(tmp_path), http, now=1060, log=lambda *_: None, cli=cli)
    assert len(cli.posted) == 2 and not cli.cancelled                         # same plan: nothing re-posted
    st = json.loads((tmp_path / real_live.FILE).read_text())
    assert st['mode'] == 'live' and 'not-a-real-key' not in json.dumps(st)  # the key is never written anywhere


def test_cancels_orders_the_plan_no_longer_wants(tmp_path, on, monkeypatch):
    cli, http = FakeClient(), FakeHTTP()
    real_live.tick(str(tmp_path), http, now=1000, log=lambda *_: None, cli=cli)
    moved = json.loads(json.dumps(PLAN))
    moved['orders'][0]['price'] = 0.30
    monkeypatch.setattr(real_live.real, 'compute', lambda *a, **k: json.loads(json.dumps(moved)))
    real_live.tick(str(tmp_path), http, now=1060, log=lambda *_: None, cli=cli)
    assert cli.cancelled == ['o1'] and ('Y1', 0.30, 20.0) in cli.posted


def test_placing_orders_never_looks_like_a_loss(tmp_path, on):
    cli, http = FakeClient(cash=40.0), FakeHTTP()
    real_live.tick(str(tmp_path), http, now=1000, log=lambda *_: None, cli=cli)
    cli.cash = 3.2                                                            # even if the balance drops by what the orders hold
    real_live.tick(str(tmp_path), http, now=1060, log=lambda *_: None, cli=cli)
    assert json.loads((tmp_path / real_live.FILE).read_text())['mode'] == 'live'


def test_loss_limit_halts_and_cancels_everything(tmp_path, on):
    cli, http = FakeClient(cash=40.0), FakeHTTP()
    real_live.tick(str(tmp_path), http, now=1000, log=lambda *_: None, cli=cli)
    http.positions = [{'asset': 'Y1', 'size': 20, 'currentValue': 0.4, 'cashPnl': -5.2, 'realizedPnl': -6.0}]   # 11.2 USD lost
    real_live.tick(str(tmp_path), http, now=1060, log=lambda *_: None, cli=cli)
    st = json.loads((tmp_path / real_live.FILE).read_text())
    assert st['mode'] == 'halted' and 'förlustgränsen' in st['halt_reason'] and not cli.orders
    n = len(cli.posted)
    real_live.tick(str(tmp_path), http, now=1120, log=lambda *_: None, cli=cli)
    assert len(cli.posted) == n                                               # stays halted


def test_owner_stop_and_start_issues_others_ignored(tmp_path, on):
    cli = FakeClient()
    stranger = {'number': 1, 'title': 'riktiga pengar: stopp', 'user': {'login': 'someone'}}
    real_live.tick(str(tmp_path), FakeHTTP(issues=[stranger]), now=1000, log=lambda *_: None, cli=cli)
    assert cli.orders                                                         # a stranger cannot stop it
    http = FakeHTTP(issues=[{'number': 2, 'title': 'Riktiga pengar: stopp', 'user': {'login': 'owner'}}])
    real_live.tick(str(tmp_path), http, now=1060, log=lambda *_: None, cli=cli)
    st = json.loads((tmp_path / real_live.FILE).read_text())
    assert st['mode'] == 'halted' and cli.cancel_all_calls >= 1 and not cli.orders and http.answers
    http = FakeHTTP(issues=[{'number': 3, 'title': 'riktiga pengar: start', 'user': {'login': 'owner'}}])
    real_live.tick(str(tmp_path), http, now=1120, log=lambda *_: None, cli=cli)
    assert json.loads((tmp_path / real_live.FILE).read_text())['mode'] == 'live' and cli.orders


def test_cancel_all_command_is_harmless_without_a_key(monkeypatch, capsys):
    monkeypatch.delenv('POLY_PRIVATE_KEY', raising=False)
    assert real_live.main(['--cancel-all']) == 0
    assert 'nothing to cancel' in capsys.readouterr().out
