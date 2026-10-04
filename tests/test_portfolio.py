import json

import numpy as np
import pandas as pd
import pytest

from tradebot import portfolio as pf
from tradebot.data import synthetic_prices
from tradebot.engine import Config

ZERO = Config(fee_bps=0, slippage_bps=0)


def universe(n_days=700, coins=('BTC', 'ETH', 'XRP')):
    return pd.DataFrame({c: synthetic_prices(n_days, seed=i + 1).values for i, c in enumerate(coins)},
                        index=synthetic_prices(n_days).index)


def test_weights_are_long_only_and_never_exceed_full_exposure():
    p = universe()
    for regime in (False, True):
        w = pf.multi_signals(p, regime)
        assert (w >= 0).all().all() and (w.sum(axis=1) <= 1 + 1e-9).all()


def test_unlisted_coin_gets_no_weight_and_does_not_dilute_before_listing():
    p = universe()
    p.loc[p.index[:300], 'XRP'] = np.nan  # listed later
    w = pf.multi_signals(p, regime=False)
    assert (w['XRP'].iloc[:300] == 0).all()
    # before listing, the two live coins share the capital (divide by 2, not 3)
    raw = (p.apply(pf.ensemble_signal) * p.apply(pf.vol_scale)).iloc[250]
    assert w.iloc[250, :2].to_numpy() == pytest.approx(raw.iloc[:2].to_numpy() / 2)


def test_backtest_acts_next_bar_and_charges_costs():
    idx = pd.date_range('2024', periods=4, freq='D', tz='UTC')
    p = pd.DataFrame({'A': [100, 110, 99, 120.0], 'B': [50, 50, 50, 50.0]}, index=idx)
    sig = pd.DataFrame({'A': [1, 0, 0, 0.0], 'B': [0, 0, 0, 0.0]}, index=idx)
    bt = pf.portfolio_backtest(p, sig, Config(fee_bps=5, slippage_bps=3))
    assert bt['gross'].iloc[1] == pytest.approx(0.10)       # bought at close 0, held bar 1
    assert bt['costs'].sum() == pytest.approx(2 * 8 / 1e4)  # in and out


@pytest.mark.parametrize('name', pf.VARIANTS)
def test_variants_are_causal(name):
    assert pf.leakage_test_multi(pf.variant_fn(name), universe(500))['status'] == 'ABSENT'


def test_leakage_test_catches_future_peeking():
    peek = lambda p: (p.shift(-1) > p).astype(float) / p.shape[1]
    assert pf.leakage_test_multi(peek, universe(400))['status'] == 'PRESENT'


def test_load_universe_reports_missing_and_splices_renamed_pairs():
    idx = pd.date_range('2019-01-01', periods=10, freq='D', tz='UTC')
    def fetch(sym, start):
        if sym == 'BCHABCUSDT':
            return pd.Series(1.0, index=idx[:6])
        if sym == 'BCHUSDT':
            return pd.Series(2.0, index=idx[4:])
        if sym in ('BTCUSDT', 'ETHUSDT'):
            return pd.Series(3.0, index=idx)
        raise ValueError('400 Invalid symbol')
    prices, info = pf.load_universe(fetch, universe={'BTC': ['BTCUSDT'], 'ETH': ['ETHUSDT'],
                                                    'BCH': ['BCHABCUSDT', 'BCHUSDT'],
                                                    'BSV': ['BCHSVUSDT']})
    assert info['missing'] == ['BSV']
    assert list(prices['BCH']) == [1, 1, 1, 1, 2, 2, 2, 2, 2, 2]


def test_gates_run_end_to_end():
    res = pf.run_portfolio_gates(universe(800), 'multi_ensemble_regime_vol', Config(), n_trials=47)
    assert res['gate1_no_leakage'] is True
    assert len(res['walk_forward']['folds']) == (800 - 180) // 60
    assert 0 <= res['avg_exposure'] <= 1


def test_research_portfolio_cli(tmp_path):
    from tradebot.__main__ import main
    csv, out = tmp_path / 'u.csv', tmp_path / 'r.json'
    universe(700).tz_localize(None).to_csv(csv)
    assert main(['research-portfolio', '--csv', str(csv), '--out', str(out)]) == 0
    res = json.loads(out.read_text())
    assert res['n_trials'] == 47 and len(res['results']) == 2
