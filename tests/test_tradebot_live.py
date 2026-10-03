import hashlib
import hmac
from urllib.parse import parse_qsl, urlencode

import pandas as pd
import pytest

from tradebot.broker import BinanceTestnetBroker, PaperBroker, SymbolRules
from tradebot.data import synthetic_prices
from tradebot.live import LiveConfig, live_returns, trade_step

APPROVAL = {'approved': True, 'params': {'lookback': 20}, 'allow_short': False,
            'oos_metrics': {'sharpe': 1.0, 'max_drawdown': -30.0}}


def rising(n=100):
    idx = pd.date_range('2024-01-01', periods=n, freq='D', tz='UTC')
    return pd.Series([100 * 1.01 ** i for i in range(n)], index=idx)


def paper(tmp_path, prices):
    b = PaperBroker(tmp_path / 'w.json', 'BTCUSDT', 10_000)
    b.set_price(float(prices.iloc[-1]))
    return b


def now_after(prices):
    return prices.index[-1] + pd.Timedelta(hours=1)


# -- rules / paper broker ----------------------------------------------------

def test_round_qty_rounds_down_to_step():
    r = SymbolRules('BTC', 'USDT', step_size=0.001)
    assert r.round_qty(0.12399) == pytest.approx(0.123)


def test_paper_broker_charges_costs_and_persists(tmp_path):
    b = PaperBroker(tmp_path / 'w.json', 'BTCUSDT', 10_000, fee_bps=5, slippage_bps=3)
    b.set_price(100)
    fill = b.market_order('BUY', 10)
    assert fill.price == pytest.approx(100.03)
    assert b.balances()['BTC'] == 10
    again = PaperBroker(tmp_path / 'w.json', 'BTCUSDT')
    assert again.balances()['USDT'] == pytest.approx(10_000 - 1000.3 - 0.50015)
    with pytest.raises(ValueError):
        b.market_order('SELL', 11)


# -- testnet broker ----------------------------------------------------------

class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self.data


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, params, headers, timeout):
        self.calls.append((method, url, dict(params), headers))
        path = url.split('testnet.binance.vision')[1]
        return Resp(self.routes[(method, path)])


EXCHANGE_INFO = {'symbols': [{'baseAsset': 'BTC', 'quoteAsset': 'USDT', 'filters': [
    {'filterType': 'LOT_SIZE', 'minQty': '0.00001', 'stepSize': '0.00001'},
    {'filterType': 'NOTIONAL', 'minNotional': '5.0'}]}]}


def test_testnet_refuses_mainnet():
    with pytest.raises(ValueError, match='testnet'):
        BinanceTestnetBroker('BTCUSDT', 'k', 's', base_url='https://api.binance.com')


def test_testnet_requires_keys(monkeypatch):
    monkeypatch.delenv('BINANCE_TESTNET_API_KEY', raising=False)
    monkeypatch.delenv('BINANCE_TESTNET_API_SECRET', raising=False)
    with pytest.raises(ValueError):
        BinanceTestnetBroker('BTCUSDT')


def test_testnet_signs_orders_and_parses_fill():
    http = FakeHttp({
        ('GET', '/api/v3/exchangeInfo'): EXCHANGE_INFO,
        ('POST', '/api/v3/order'): {
            'executedQty': '0.01000', 'cummulativeQuoteQty': '600.0',
            'fills': [{'price': '60000', 'qty': '0.01', 'commission': '0.6',
                       'commissionAsset': 'USDT'}]},
    })
    b = BinanceTestnetBroker('BTCUSDT', 'key', 'secret', session=http)
    fill = b.market_order('BUY', 0.01)
    assert fill.price == 60000 and fill.commission_quote == 0.6

    method, _, params, headers = next(c for c in http.calls if c[0] == 'POST')
    assert headers['X-MBX-APIKEY'] == 'key'
    sig = params.pop('signature')
    expected = hmac.new(b'secret', urlencode(params).encode(), hashlib.sha256).hexdigest()
    assert sig == expected
    assert params['type'] == 'MARKET' and params['quantity'] == '0.01'


def test_testnet_rules_and_balances():
    http = FakeHttp({
        ('GET', '/api/v3/exchangeInfo'): EXCHANGE_INFO,
        ('GET', '/api/v3/account'): {'balances': [
            {'asset': 'BTC', 'free': '1.5', 'locked': '0'},
            {'asset': 'USDT', 'free': '100', 'locked': '0'}]},
    })
    b = BinanceTestnetBroker('BTCUSDT', 'k', 's', session=http)
    r = b.rules()
    assert (r.step_size, r.min_notional) == (0.00001, 5.0)
    assert b.balances() == {'BTC': 1.5, 'USDT': 100.0}


# -- live loop ---------------------------------------------------------------

def test_dry_run_plans_buy_without_changing_anything(tmp_path):
    p = rising()
    b, state = paper(tmp_path, p), {}
    rep = trade_step(b, p, APPROVAL, state, now=now_after(p))
    assert rep['action'] == 'BUY' and not rep['executed']
    assert state == {} and b.balances()['BTC'] == 0


def test_execute_buys_capped_size_and_is_idempotent(tmp_path):
    p = rising()
    b, state = paper(tmp_path, p), {}
    rep = trade_step(b, p, APPROVAL, state, execute=True, now=now_after(p))
    assert rep['action'] == 'BUY' and rep['executed']
    notional = b.balances()['BTC'] * float(p.iloc[-1])
    assert notional <= 0.20 * 10_000 + 1e-6
    assert state['last_bar'] == str(p.index[-1])
    # same bar again: nothing happens
    assert trade_step(b, p, APPROVAL, state, execute=True, now=now_after(p))['action'] == 'SKIP'


def test_sells_when_signal_turns_off(tmp_path):
    up = rising()
    b, state = paper(tmp_path, up), {}
    trade_step(b, up, APPROVAL, state, execute=True, now=now_after(up))
    last = float(up.iloc[-1])
    # flat, then a gentle 25-bar decline: momentum turns off without a crash
    vals = [last] * 75 + [last * 0.997 ** i for i in range(1, 26)]
    down = pd.Series(vals, index=up.index + pd.Timedelta(days=100))
    b.set_price(float(down.iloc[-1]))
    rep = trade_step(b, down, APPROVAL, state, execute=True, now=now_after(down))
    assert rep['action'] == 'SELL'
    assert b.balances()['BTC'] == pytest.approx(0, abs=1e-6)


def test_refuses_unapproved_short_and_stale(tmp_path):
    p = rising()
    b = paper(tmp_path, p)
    assert trade_step(b, p, {**APPROVAL, 'approved': False}, {}, now=now_after(p))['action'] == 'REFUSED'
    assert trade_step(b, p, {**APPROVAL, 'approved': False}, {}, ignore_gates=True,
                      now=now_after(p))['action'] == 'BUY'
    assert trade_step(b, p, {**APPROVAL, 'allow_short': True}, {}, now=now_after(p))['action'] == 'REFUSED'
    late = p.index[-1] + pd.Timedelta(days=5)
    assert 'stale' in trade_step(b, p, APPROVAL, {}, now=late)['reason']


def test_halt_flattens_and_stays_halted(tmp_path):
    p = rising()
    b, state = paper(tmp_path, p), {}
    b.wallet = {'BTC': 10.0, 'USDT': 10_000.0}
    # previous equity much higher at full exposure -> drawdown beyond 1.5x backtest
    state['history'] = [{'ts': 'x', 'equity': 40_000.0, 'exposure': 1.0}]
    rep = trade_step(b, p, APPROVAL, state, execute=True, now=now_after(p))
    assert rep['action'] == 'HALT'
    assert 'DRAWDOWN_EXCEEDED' in rep['health']['alerts']
    assert b.balances()['BTC'] == pytest.approx(0, abs=1e-6)
    assert state['halted']
    later = p.copy()
    later.index = later.index + pd.Timedelta(days=1)
    assert trade_step(b, later, APPROVAL, state, now=now_after(later))['action'] == 'HALTED'


def test_live_returns_scale_by_exposure():
    hist = [{'equity': 100, 'exposure': 0.2}, {'equity': 101, 'exposure': 0.0},
            {'equity': 101, 'exposure': 0.0}]
    assert list(live_returns(hist)) == pytest.approx([0.05, 0.0])


def test_cli_run_then_trade_on_paper(tmp_path, monkeypatch, capsys):
    from tradebot import __main__ as cli

    p = synthetic_prices(900, seed=3)
    csv = tmp_path / 'p.csv'
    pd.DataFrame({'timestamp': p.index, 'close': p.values}).to_csv(csv, index=False)
    approval = tmp_path / 'approval.json'
    cli.main(['run', '--csv', str(csv), '--save-approval', str(approval)])
    assert approval.exists()

    recent = rising()
    recent.index = pd.date_range(end=pd.Timestamp.now(tz='UTC').floor('D'), periods=100,
                                 freq='D', tz='UTC')
    monkeypatch.setattr(cli, 'fetch_binance', lambda *a, **k: recent)
    args = ['trade', '--approval', str(approval), '--state', str(tmp_path / 's.json'),
            '--paper-wallet', str(tmp_path / 'w.json'), '--ignore-gates', '--execute']
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert '"executed": true' in out
    assert (tmp_path / 's.json').exists()


def test_approval_contains_dashboard_fields(tmp_path):
    import json
    from tradebot import __main__ as cli

    p = synthetic_prices(700, seed=4)
    csv = tmp_path / 'p.csv'
    pd.DataFrame({'timestamp': p.index, 'close': p.values}).to_csv(csv, index=False)
    out = tmp_path / 'a.json'
    cli.main(['run', '--csv', str(csv), '--save-approval', str(out)])
    a = json.loads(out.read_text())
    assert set(a['gates']) == {'no_leakage', 'deflated_sharpe', 'walk_forward'}
    assert len(a['critic']) == 8
    assert a['walk_forward']['folds'] and 'sharpe' in a['walk_forward']['folds'][0]
    assert set(a['regimes']) == {'bull', 'bear', 'chop'}


def test_state_keeps_account_snapshot_after_skip(tmp_path):
    p = rising()
    b, state = paper(tmp_path, p), {}
    trade_step(b, p, APPROVAL, state, execute=True, now=now_after(p))
    acct = state['account']
    assert acct['base'] == pytest.approx(b.balances()['BTC'])
    assert acct['quote'] == pytest.approx(b.balances()['USDT'])
    assert state['last_decision']['action'] == 'BUY'
    trade_step(b, p, APPROVAL, state, execute=True, now=now_after(p))  # SKIP
    assert state['account'] == acct
