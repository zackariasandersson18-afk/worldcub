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
