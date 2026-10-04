import numpy as np
import pandas as pd
import pytest

from tradebot import strategies as st
from tradebot.critic import leakage_test
from tradebot.data import synthetic_prices
from tradebot.live import trade_step


def test_regime_filter_follows_200_day_average():
    idx = pd.date_range('2020', periods=400, freq='D', tz='UTC')
    up = pd.Series(np.linspace(100, 300, 400), index=idx)
    f = st.regime_filter(up)
    assert (f.iloc[:199] == 0).all() and (f.iloc[250:] == 1).all()
    assert (st.regime_filter(up[::-1].set_axis(idx)).iloc[250:] == 0).all()


def test_vol_scale_is_capped_and_shrinks_with_volatility():
    calm = synthetic_prices(300, seed=1, vol=0.005)
    wild = synthetic_prices(300, seed=1, vol=0.08)
    assert st.vol_scale(calm).iloc[-1] == 1.0
    assert 0 < st.vol_scale(wild).iloc[-1] < 0.5
    assert st.vol_scale(wild).max() <= 1.0


def test_ensemble_is_share_of_positive_lookbacks():
    idx = pd.date_range('2020', periods=300, freq='D', tz='UTC')
    assert st.ensemble_signal(pd.Series(np.linspace(100, 200, 300), index=idx)).iloc[-1] == 1.0
    assert st.ensemble_signal(pd.Series(np.linspace(200, 100, 300), index=idx)).iloc[-1] == 0.0


@pytest.mark.parametrize('name', st.STRATEGIES)
def test_every_strategy_is_causal_and_bounded(name):
    p = synthetic_prices(600, seed=5)
    fitted = st.make_fit(name)(p.iloc[:180])
    sig = fitted['signal_fn'](p)
    assert sig.between(0, 1).all()
    assert leakage_test(fitted['signal_fn'], p)['status'] == 'ABSENT'


def test_unknown_strategy_rejected():
    with pytest.raises(ValueError):
        st.build_signal('nope')


def test_live_step_uses_the_approved_strategy(tmp_path):
    from tradebot.broker import PaperBroker
    idx = pd.date_range(end='2024-04-10', periods=300, freq='D', tz='UTC')
    p = pd.Series(np.linspace(100, 200, 300), index=idx)
    b = PaperBroker(tmp_path / 'w.json', 'BTCUSDT', 10_000)
    b.set_price(float(p.iloc[-1]))
    approval = {'approved': True, 'strategy': 'ensemble_regime_vol', 'params': {},
                'oos_metrics': {'sharpe': 1.0, 'max_drawdown': -30.0}}
    rep = trade_step(b, p, approval, {}, now=p.index[-1] + pd.Timedelta(hours=1))
    expected = float(st.build_signal('ensemble_regime_vol')(p).iloc[-1])
    assert rep['signal'] == pytest.approx(expected) and rep['action'] == 'BUY'


def test_research_cli_tests_every_variant(tmp_path, capsys):
    import json
    from tradebot.__main__ import main
    p = synthetic_prices(700, seed=8)
    csv = tmp_path / 'p.csv'
    pd.DataFrame({'timestamp': p.index, 'close': p.values}).to_csv(csv, index=False)
    out = tmp_path / 'r.json'
    assert main(['research', '--csv', str(csv), '--out', str(out)]) == 0
    res = json.loads(out.read_text())
    assert [r['strategy'] for r in res['results']] == list(st.STRATEGIES)
    assert res['n_trials'] == 25 + st.NEW_VARIANT_TRIALS
