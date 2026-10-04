import numpy as np
import pandas as pd
import pytest

from footbot import backtest, data, model


def synthetic(n_weeks=140, seed=0):
    rng = np.random.default_rng(seed)
    teams = [f'T{i}' for i in range(10)]
    att = dict(zip(teams, np.linspace(-0.4, 0.4, 10)))
    rows = []
    start = pd.Timestamp('2018-07-07')
    for w in range(n_weeks):
        order = rng.permutation(teams)
        for h, a in zip(order[:5], order[5:]):
            lam, mu = np.exp(0.25 + 0.2 + att[h] - att[a]), np.exp(0.25 + att[a] - att[h])
            rows.append({'league': 'E0', 'season': 'x', 'date': start + pd.Timedelta(weeks=w),
                         'home': h, 'away': a, 'hg': rng.poisson(lam), 'ag': rng.poisson(mu),
                         'odds_H': 2.0, 'odds_D': 3.5, 'odds_A': 4.0, 'odds_O2.5': 1.9, 'odds_U2.5': 1.9,
                         'close_H': 2.0, 'close_D': 3.5, 'close_A': 4.0, 'close_O2.5': 1.9, 'close_U2.5': 1.9,
                         'hc': rng.poisson(5.5), 'ac': rng.poisson(4.5)})
    return pd.DataFrame(rows), att


def test_fit_recovers_strengths():
    df, att = synthetic()
    g = model.fit(list(df.home), list(df.away), df.hg, df.ag, np.zeros(len(df)))
    est = [g.attack[g.teams[t]] for t in sorted(att)]
    assert np.corrcoef(est, [att[t] for t in sorted(att)])[0, 1] > 0.9
    assert 0.1 < g.home < 0.3
    assert -0.2 <= g.rho <= 0.2


def test_markets_are_consistent():
    p = model.markets(model.score_matrix(1.5, 1.1, -0.05))
    assert p['H'] + p['D'] + p['A'] == pytest.approx(1)
    assert p['O2.5'] + p['U2.5'] == pytest.approx(1)
    assert p['O0.5'] > p['O1.5'] > p['O2.5'] > p['O3.5']
    assert model.total_over(5, 5, 9.5) == pytest.approx(1 - 0.4579, abs=1e-3)


def test_fair_and_pick():
    assert backtest.fair([2.0, 2.0]) == [0.5, 0.5]
    assert backtest.fair([2.0, float('nan')]) is None
    probs = {'H': 0.6, 'D': 0.2, 'A': 0.2, 'O2.5': 0.5, 'U2.5': 0.5}
    b = backtest.pick(probs, {'odds_H': 2.0, 'odds_D': 3.0, 'odds_A': 6.0, 'odds_O2.5': 1.9, 'odds_U2.5': float('nan')})
    assert b['side'] == 'H' and b['edge'] == pytest.approx(0.2)
    assert b['stake'] == pytest.approx(0.02)            # capped
    assert backtest.pick({**probs, 'H': 0.5}, {'odds_H': 2.0}) is None   # edge 0
    assert backtest.pick({**probs, 'A': 0.3}, {'odds_A': 6.0}) is None   # odds above cap


def test_clv_and_won():
    assert backtest.clv('H', 2.2, {'close_H': 2.0, 'close_D': 4.0, 'close_A': 4.0}) == pytest.approx(0.1)
    assert backtest.won('U2.5', 1, 1) and not backtest.won('O2.5', 1, 1) and backtest.won('D', 2, 2)


def test_run_without_leakage(monkeypatch):
    df, _ = synthetic()
    seen = []
    real = model.fit

    def spy(home, away, hg, ag, age, **kw):
        seen.append(min(age))
        return real(home, away, hg, ag, age, **kw)
    monkeypatch.setattr(model, 'fit', spy)
    res = backtest.run(df, log=lambda *_: None)
    assert min(seen) >= 1                # every training match is before the Monday cutoff
    assert res['diagnostics']['1x2']['n'] > 0 and res['diagnostics']['corners']['n'] > 0
    assert res['n_bets'] > 0


def test_tidy_main_parses_two_digit_years():
    raw = pd.DataFrame({'Date': ['10/08/19', '11/08/2019'], 'HomeTeam': ['A ', 'B'], 'AwayTeam': ['B', 'A'],
                        'FTHG': [1, 2], 'FTAG': [0, 2], 'PSH': [2.0, 2.1], 'PC>2.5': [1.9, 1.8], 'HC': [5, 6]})
    t = data.tidy_main(raw, 'E0', '1920')
    assert list(t['date'].dt.year) == [2019, 2019] and t['home'][0] == 'A'
    assert t['close_O2.5'][1] == 1.8 and t['odds_D'].isna().all()
