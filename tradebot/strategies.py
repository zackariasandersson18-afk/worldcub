"""Example strategy: time-series momentum.

Hypothesis (mechanism, not pattern):
    Crypto investors underreact to new information at first and then
    chase it: flows into a coin that has risen arrive over weeks, not at
    once (retail inflows, slow funds rebalancing, forced short covering
    in perpetual futures). The counterparty is the late or forced trader
    who buys after the move and the short who gets liquidated into it.
    If those flows disappear (e.g. markets become more efficient) the
    edge disappears too.

Rules:
    entry:        go long when the close is above the close `lookback`
                  bars ago (short as well if allow_short).
    exit:         go flat/flip when the lookback return turns negative.
    invalidation: walk-forward worst fold below the gate, or the live
                  health check fires.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tradebot.engine import Config, backtest, metrics

LOOKBACK_GRID = (20, 40, 60, 90, 120)


def momentum_signal(history: pd.Series, lookback: int,
                    allow_short: bool = False) -> pd.Series:
    """Signal at t uses only closes up to and including t."""
    past_return = np.log(history / history.shift(lookback))
    signal = np.sign(past_return)
    if not allow_short:
        signal = signal.clip(lower=0)
    return signal.fillna(0)


def fit_momentum(train: pd.Series, cfg: Config | None = None,
                 grid: tuple[int, ...] = LOOKBACK_GRID,
                 allow_short: bool = False) -> dict:
    """Choose the lookback with the best Sharpe on the training window only."""
    cfg = cfg or Config()
    best_lb, best_sharpe = grid[0], -np.inf
    for lb in grid:
        if lb >= len(train) - 10:
            continue
        bt = backtest(train, momentum_signal(train, lb, allow_short), cfg)
        m = metrics(bt['net'].iloc[lb:], cfg, min_obs=10)
        sharpe = m.get('sharpe', -np.inf)
        if sharpe > best_sharpe:
            best_lb, best_sharpe = lb, sharpe

    return {
        'signal_fn': lambda h, lb=best_lb: momentum_signal(h, lb, allow_short),
        'params': {'lookback': best_lb},
        'n_trials': len(grid),
    }


# ---------------------------------------------------------------------------
# Pre-registered variants (fixed BEFORE looking at their results).
#
#   momentum             baseline above
#   momentum_regime      momentum, but only while the close is above its
#                        200-day average: stay in cash in bear markets
#   momentum_voltarget   momentum, position scaled to TARGET_VOL annualized
#                        volatility (never above the baseline's full size)
#   ensemble             no fitted parameter: the fraction of lookbacks in
#                        LOOKBACK_GRID whose return is positive
#   ensemble_regime_vol  ensemble x regime filter x volatility scaling
#
# Each modifier has fixed constants; only the momentum lookback is ever fitted,
# and only on the training window.
# ---------------------------------------------------------------------------
TREND_MA = 200
TARGET_VOL = 0.40
VOL_WINDOW = 30
PERIODS_PER_YEAR = 365
NEW_VARIANT_TRIALS = 4 * len(LOOKBACK_GRID)  # counted honestly: 4 new ideas x 5 lookbacks


def regime_filter(history: pd.Series, ma: int = TREND_MA) -> pd.Series:
    """1 while the close is above its `ma`-day average, else 0 (0 during warm-up)."""
    sma = history.rolling(ma).mean()
    return (history > sma).astype(float)


def vol_scale(history: pd.Series, target: float = TARGET_VOL, window: int = VOL_WINDOW,
              periods_per_year: int = PERIODS_PER_YEAR) -> pd.Series:
    """Position multiplier in [0, 1]: target / realized volatility, capped at 1."""
    vol = history.pct_change().rolling(window).std() * np.sqrt(periods_per_year)
    return (target / vol).clip(upper=1.0).fillna(0.0)


def ensemble_signal(history: pd.Series, lookbacks: tuple[int, ...] = LOOKBACK_GRID) -> pd.Series:
    """Share of lookbacks with a positive past return, in [0, 1]."""
    votes = [(np.log(history / history.shift(lb)) > 0).astype(float) for lb in lookbacks]
    return sum(votes) / len(lookbacks)


def build_signal(name: str, params: dict | None = None):
    """signal_fn(history) for a strategy name and its fitted params."""
    params = params or {}
    lb = params.get('lookback')
    if name == 'momentum':
        return lambda h: momentum_signal(h, lb)
    if name == 'momentum_regime':
        return lambda h: momentum_signal(h, lb) * regime_filter(h)
    if name == 'momentum_voltarget':
        return lambda h: momentum_signal(h, lb) * vol_scale(h)
    if name == 'ensemble':
        return ensemble_signal
    if name == 'ensemble_regime_vol':
        return lambda h: ensemble_signal(h) * regime_filter(h) * vol_scale(h)
    raise ValueError(f'unknown strategy {name!r}; choose from {sorted(STRATEGIES)}')


def make_fit(name: str):
    """fit(train) -> {'signal_fn', 'params'}. Lookback strategies pick the lookback
    with plain momentum on the training window (the filters have no free
    parameter, and a 180-day window is too short for the 200-day average)."""
    def fit(train: pd.Series, cfg: Config | None = None, allow_short: bool = False) -> dict:
        if name.startswith('ensemble'):
            params = {}
        else:
            params = fit_momentum(train, cfg)['params']
        return {'signal_fn': build_signal(name, params), 'params': params,
                'n_trials': len(LOOKBACK_GRID)}
    return fit


STRATEGIES = ('momentum', 'momentum_regime', 'momentum_voltarget',
              'ensemble', 'ensemble_regime_vol')
