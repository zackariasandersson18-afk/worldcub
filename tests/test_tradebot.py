import numpy as np
import pandas as pd
import pytest

from tradebot import strategies
from tradebot.__main__ import main
from tradebot.critic import leakage_test, review
from tradebot.data import check_alignment, fetch_binance, load_csv, normalize, synthetic_prices
from tradebot.engine import Config, backtest, metrics
from tradebot.gates import run_gates
from tradebot.monitor import health_check
from tradebot.regimes import label_regimes, regime_report
from tradebot.sizing import losing_streak_drawdown, position_size
from tradebot.stats import deflated_sharpe, walk_forward

ZERO_COST = Config(fee_bps=0, slippage_bps=0)


def prices(n=600, seed=1):
    return synthetic_prices(n=n, seed=seed)


# -- engine -----------------------------------------------------------------

def test_signal_is_acted_on_next_bar():
    p = pd.Series([100, 110, 99, 120.0], index=pd.date_range('2024', periods=4, tz='UTC'))
    # perfect foresight: long exactly on up-bars. Unshifted this would print money.
    foresight = (p.pct_change().shift(0) > 0).astype(float)
    bt = backtest(p, foresight, ZERO_COST)
    assert list(bt['position']) == [0, 0, 1, 0]
    assert bt['gross'].iloc[2] == pytest.approx(99 / 110 - 1)


def test_costs_charged_on_turnover_with_fee_and_slippage():
    p = pd.Series(100.0, index=pd.date_range('2024', periods=5, tz='UTC'))
    signal = pd.Series([1, 1, -1, -1, -1.0], index=p.index)
    bt = backtest(p, signal, Config(fee_bps=5, slippage_bps=3))
    # enter 1 unit (turnover 1), flip to -1 (turnover 2)
    assert bt['costs'].sum() == pytest.approx(3 * 8 / 1e4)
    assert bt['equity'].iloc[-1] < 10_000


def test_leverage_is_clipped():
    p = prices(50)
    bt = backtest(p, pd.Series(5.0, index=p.index), Config(max_leverage=1.0))
    assert bt['position'].max() == 1.0


def test_metrics():
    cfg = Config()
    assert metrics(pd.Series(np.zeros(50)), cfg)['error'] == 'insufficient_data'
    r = pd.Series([0.01, -0.02, 0.005] * 50)
    m = metrics(r, cfg)
    assert m['max_drawdown'] < 0
    assert m['longest_dd_periods'] >= 1
    assert m['n_obs'] == 150


# -- multiple testing --------------------------------------------------------

def test_dsr_rejects_best_of_many_noisy_trials():
    # the document's example: 80 variations, best Sharpe 1.8, three years daily
    res = deflated_sharpe(1.8, n_trials=80, n_obs=1095)
    assert res['verdict'] == 'REJECT'
    assert res['expected_max_from_noise'] > 1.0


def test_dsr_passes_strong_single_trial_and_falls_with_more_trials():
    one = deflated_sharpe(2.0, n_trials=1, n_obs=1825)
    many = deflated_sharpe(2.0, n_trials=500, n_obs=1825)
    assert one['verdict'] == 'PASS'
    assert many['deflated_sharpe'] < one['deflated_sharpe']


def test_dsr_matches_noise_simulation():
    # best of N pure-noise Sharpes should land near expected_max_from_noise
    rng = np.random.default_rng(0)
    n_trials, n_obs = 50, 730
    best = [max(r.mean() / r.std() * np.sqrt(365)
                for r in rng.normal(0, 0.02, (n_trials, n_obs)))
            for _ in range(40)]
    expected = deflated_sharpe(1.0, n_trials, n_obs)['expected_max_from_noise']
    assert abs(np.mean(best) - expected) < 0.3


# -- walk-forward ------------------------------------------------------------

def test_walk_forward_fits_on_train_only():
    p = prices(500)
    seen = []

    def fit(train):
        seen.append(train.index[-1])
        return {'signal_fn': lambda h: strategies.momentum_signal(h, 20)}

    wf = walk_forward(p, fit, ZERO_COST, train_days=180, test_days=60)
    assert len(wf['folds']) == 5
    for train_end, start in zip(seen, wf['folds']['start']):
        assert train_end < start
    assert len(wf['oos_net']) == 5 * 60
    assert not wf['oos_net'].index.duplicated().any()


def test_walk_forward_needs_enough_data():
    with pytest.raises(ValueError):
        walk_forward(prices(100), strategies.fit_momentum, ZERO_COST)


# -- critic ------------------------------------------------------------------

def centered_ma_signal(h):
    ma = h.rolling(11, center=True).mean()
    return (h > ma).astype(float)


def test_leakage_test_catches_future_data():
    p = prices(400)
    assert leakage_test(centered_ma_signal, p)['status'] == 'PRESENT'
    assert leakage_test(lambda h: (h.shift(-1) > h).astype(float), p)['status'] == 'PRESENT'
    assert leakage_test(lambda h: (h > h.mean()).astype(float), p)['status'] == 'PRESENT'


def test_leakage_test_passes_causal_signal():
    p = prices(400)
    assert leakage_test(lambda h: strategies.momentum_signal(h, 30), p)['status'] == 'ABSENT'


def test_review_quotes_problems():
    bad = '\n'.join([
        'ma = prices.rolling(20, center=True).mean()',
        'position = signal',
        'costs = turnover * fee_bps / 1e4',
        'fill = df["high"]',
    ])
    items = {it['item']: it for it in review(bad)}
    assert items[1]['status'] == 'PRESENT'
    assert items[3]['status'] == 'PRESENT'
    assert 'center=True' in items[3]['quotes'][0]
    assert items[4]['status'] == 'PRESENT'
    assert items[5]['status'] == 'CHECK'


def test_review_checks_sample_and_alignment():
    naive = pd.Series(prices(400).values, index=pd.date_range('2020', periods=400))
    items = {it['item']: it for it in review('', prices=naive)}
    assert items[8]['status'] == 'PRESENT'
    assert items[7]['status'] in ('ABSENT', 'PRESENT')


def test_gate1_fails_leaky_strategy():
    res = run_gates(prices(500), lambda tr: {'signal_fn': centered_ma_signal},
                    ZERO_COST, n_trials=1)
    assert not res['gate1_no_leakage']
    assert not res['approved']


# -- regimes -----------------------------------------------------------------

def test_regimes():
    p = prices(800)
    labels = label_regimes(p)
    assert (labels.iloc[:199] == 'warmup').all()
    rep = regime_report(p, p.pct_change().fillna(0), Config())
    assert set(rep['regimes']) == {'bull', 'bear', 'chop'}
    assert rep['has_bull_and_bear']


# -- sizing ------------------------------------------------------------------

def test_position_size_risks_one_percent():
    res = position_size(10_000, entry=100, stop=95)
    assert res['loss_if_stopped'] == pytest.approx(100)
    assert res['units'] == pytest.approx(20)


def test_position_size_capped():
    res = position_size(10_000, entry=100, stop=99.9)
    assert res['notional'] == 2_000
    assert res['pct_of_capital'] == 20.0


def test_position_size_rejects_stop_at_entry():
    with pytest.raises(ValueError):
        position_size(10_000, 100, 100)


def test_losing_streak():
    assert losing_streak_drawdown(0.01, 12) == pytest.approx(11.4)
    assert losing_streak_drawdown(0.05, 12) == pytest.approx(46.0)


# -- monitor -----------------------------------------------------------------

def test_health_check_halts_on_decay_and_drawdown():
    bt = {'sharpe': 1.5, 'max_drawdown': -10.0}
    bad = pd.Series([-0.01] * 30)
    res = health_check(bad, bt)
    assert res['action'] == 'HALT'
    assert set(res['alerts']) == {'SHARPE_DECAY', 'DRAWDOWN_EXCEEDED'}

    good = pd.Series([0.01, 0.005, -0.002] * 10)
    assert health_check(good, bt)['action'] == 'CONTINUE'


# -- data --------------------------------------------------------------------

def test_load_csv_normalizes_to_utc(tmp_path):
    f = tmp_path / 'p.csv'
    f.write_text('timestamp,close\n2024-01-02,101\n2024-01-01,100\n2024-01-02,102\n')
    s = load_csv(str(f))
    assert str(s.index.tz) == 'UTC'
    assert list(s) == [100.0, 102.0]


def test_normalize_converts_other_timezones():
    idx = pd.date_range('2024-01-01', periods=3, freq='D', tz='America/New_York')
    s = normalize(pd.Series([1.0, 2, 3], index=idx))
    assert s.index[0].hour == 5


def test_check_alignment_flags_mixed_bars():
    daily = synthetic_prices(60)
    hourly = pd.Series(1.0, index=pd.date_range('2020', periods=60, freq='h', tz='UTC'))
    assert check_alignment(daily) == []
    assert any('bar sizes' in p for p in check_alignment(daily, hourly))


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, params, timeout):
        return FakeResponse(self.pages.pop(0) if self.pages else [])


def test_fetch_binance_uses_close_time_and_drops_open_bar():
    day = 86_400_000
    t0 = 1_704_067_200_000  # 2024-01-01
    def kline(i, close):
        return [t0 + i * day, '0', '0', '0', str(close), '0', t0 + (i + 1) * day - 1]
    future_open = [kline(10**6, 999)]  # far-future, not closed
    s = fetch_binance(start='2024-01-01', end='2024-01-04',
                      session=FakeSession([[kline(0, 100), kline(1, 101)] + future_open]))
    assert list(s) == [100.0, 101.0]
    assert s.index[0] == pd.Timestamp('2024-01-02', tz='UTC')


# -- CLI ---------------------------------------------------------------------

def test_cli_commands(capsys):
    assert main(['size', '--capital', '10000', '--entry', '100', '--stop', '95']) == 0
    assert main(['dsr', '--sharpe', '1.8', '--n-trials', '80', '--n-obs', '1095']) == 0
    assert 'REJECT' in capsys.readouterr().out
    assert main(['prompt', 'critic']) == 0
    assert 'Do not summarize' in capsys.readouterr().out


def test_cli_demo_runs_gates(capsys):
    code = main(['demo'])
    out = capsys.readouterr().out
    assert code in (0, 1)
    assert '== Gates ==' in out


def test_drawdown_counts_loss_on_first_period():
    m = metrics(pd.Series([-0.5] + [0.0] * 120), Config())
    assert m['max_drawdown'] == pytest.approx(-50.0)
    assert health_check(pd.Series([-0.5]), {'sharpe': 1, 'max_drawdown': -20})['action'] == 'HALT'
