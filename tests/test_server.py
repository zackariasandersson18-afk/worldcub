import asyncio
import json
import subprocess

import pandas as pd
import pytest

from tradebot.broker import PaperBroker
from tradebot.server import LiveTrader, ServerConfig, StateSync, price_stream

APPROVAL = {'approved': True, 'params': {'lookback': 20}, 'allow_short': False,
            'oos_metrics': {'sharpe': 1.0, 'max_drawdown': -10.0}}
T0 = pd.Timestamp('2024-04-10 12:00', tz='UTC')


def rising(end_day='2024-04-10', n=100):
    idx = pd.date_range(end=end_day, periods=n, freq='D', tz='UTC')
    return pd.Series([100 * 1.01 ** i for i in range(n)], index=idx)


class FakeSync:
    def __init__(self):
        self.messages = []

    def push(self, message):
        self.messages.append(message)
        return True


def make(tmp_path, state=None, prices=None, approval=APPROVAL):
    (tmp_path / 'trade_state.json').write_text(json.dumps(state or {}))
    broker = PaperBroker(tmp_path / 'paper_wallet.json', 'BTCUSDT', 10_000)
    sync = FakeSync()
    calls = []

    def fetch():
        calls.append(1)
        return prices if prices is not None else rising()
    t = LiveTrader(broker, approval, tmp_path, fetch, sync,
                   scfg=ServerConfig(save_s=0, heartbeat_s=3600))
    return t, broker, sync, calls


def holding(tmp_path, stop=90.0, qty=10.0, price=100.0, **kw):
    state = {'account': {'base': qty, 'quote': 9_000.0, 'price': price},
             'stop': stop, 'last_bar': '2024-04-10 00:00:00+00:00',
             'history': [{'ts': 'd', 'equity': 10_000.0, 'exposure': 0.1}]}
    t, b, s, c = make(tmp_path, state, **kw)
    b.wallet = {'BTC': qty, 'USDT': 9_000.0}
    return t, b, s, c


def test_no_action_above_stop(tmp_path):
    t, b, sync, _ = holding(tmp_path)
    assert t.on_tick(95.0, T0) is None
    assert b.balances()['BTC'] == 10
    assert t.state['server']['watching'] is True


def test_stop_sells_everything_immediately_and_pushes(tmp_path):
    t, b, sync, _ = holding(tmp_path)
    ev = t.on_tick(89.5, T0)
    assert ev['kind'] == 'STOP'
    assert b.balances()['BTC'] == pytest.approx(0)
    assert t.state['stop'] is None and t.state['account']['base'] == pytest.approx(0)
    assert t.state['trades'][-1]['reason'] == 'STOP'
    assert any('STOP' in m for m in sync.messages)
    assert json.loads((tmp_path / 'trade_state.json').read_text())['trades']
    # flat now: further drops do nothing
    assert t.on_tick(50.0, T0) is None


def test_intraday_kill_switch_halts(tmp_path):
    # no stop, 100 % exposure: a 20 % drop is beyond 1.5 x the -10 % backtest drawdown
    t, b, sync, _ = holding(tmp_path, stop=None, qty=100.0)
    t.state['history'] = [{'ts': 'd', 'equity': 10_000.0, 'exposure': 1.0}]
    b.wallet = {'BTC': 100.0, 'USDT': 0.0}
    t.state['account']['quote'] = 0.0
    ev = t.on_tick(80.0, T0)
    assert ev['kind'] == 'HALT'
    assert t.state['halted'] and b.balances()['BTC'] == pytest.approx(0)


def test_daily_step_runs_once_after_bar_close(tmp_path):
    t, b, sync, calls = make(tmp_path)
    assert t.maybe_daily(pd.Timestamp('2024-04-10 00:00:10', tz='UTC')) is None  # within delay
    rep = t.maybe_daily(pd.Timestamp('2024-04-10 00:01', tz='UTC'))
    assert rep['action'] == 'BUY' and rep['executed']
    assert t.state['stop'] and t.state['stop'] < float(rising().iloc[-1])
    assert (tmp_path / 'last_run.txt').exists()
    assert t.maybe_daily(pd.Timestamp('2024-04-10 05:00', tz='UTC')) is None
    assert len(calls) == 1


def test_daily_step_waits_for_binance_bar(tmp_path):
    t, b, sync, calls = make(tmp_path, prices=rising(end_day='2024-04-09'))
    now = pd.Timestamp('2024-04-10 00:01', tz='UTC')
    assert t.maybe_daily(now) is None
    assert t.maybe_daily(now + pd.Timedelta(seconds=10)) is None  # retry is throttled
    assert len(calls) == 1


def test_daily_refused_is_not_retried(tmp_path):
    t, b, sync, calls = make(tmp_path, approval={**APPROVAL, 'approved': False})
    now = pd.Timestamp('2024-04-10 00:01', tz='UTC')
    assert t.maybe_daily(now)['action'] == 'REFUSED'
    assert t.maybe_daily(now + pd.Timedelta(minutes=5)) is None
    assert len(calls) == 1


def test_stop_ratchets_up_only(tmp_path):
    t, b, sync, _ = make(tmp_path)
    t.maybe_daily(pd.Timestamp('2024-04-10 00:01', tz='UTC'))
    first = t.state['stop']
    t.state['stop'] = first * 1.5  # pretend an earlier, higher stop
    t._done_bar = None
    t.fetch_prices = lambda: rising(end_day='2024-04-11')
    b.set_price(float(rising(end_day='2024-04-11').iloc[-1]))
    t.maybe_daily(pd.Timestamp('2024-04-11 00:01', tz='UTC'))
    assert t.state['stop'] >= first * 1.5


def test_legacy_state_uses_last_decision_stop(tmp_path):
    state = {'account': {'base': 1.0, 'quote': 0.0}, 'last_decision': {'stop': 77.0}}
    t, *_ = make(tmp_path, state)
    assert t.state['stop'] == 77.0


def test_state_sync_commits_and_pushes(tmp_path):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[3:])
        return subprocess.CompletedProcess(cmd, 0, '', '')
    assert StateSync(tmp_path, run=run).push('msg')
    assert ['push', '-q', 'origin', 'HEAD:tradebot-state'] in calls

    def nothing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1 if 'commit' in cmd else 0, '', '')
    assert StateSync(tmp_path, run=nothing).push('msg') is False


def test_state_sync_with_real_git(tmp_path):
    remote, work = tmp_path / 'remote.git', tmp_path / 'work'
    subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True)
    subprocess.run(['git', 'init', '-q', str(work)], check=True)
    subprocess.run(['git', '-C', str(work), 'remote', 'add', 'origin', str(remote)], check=True)
    (work / 'trade_state.json').write_text('{}')
    assert StateSync(work).push('first')
    log = subprocess.run(['git', '--git-dir', str(remote), 'log', '--oneline', 'tradebot-state'],
                         capture_output=True, text=True)
    assert 'first' in log.stdout


def test_price_stream_falls_back_to_rest(monkeypatch):
    import websockets
    import tradebot.server as srv

    def boom(*a, **k):
        raise OSError('blocked')
    monkeypatch.setattr(websockets, 'connect', boom)

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {'price': '123.45'}
    monkeypatch.setattr(srv.requests, 'get', lambda *a, **k: R())

    async def first():
        async for price, ts in price_stream('BTCUSDT', poll_s=0):
            return price
    assert asyncio.run(first()) == 123.45
