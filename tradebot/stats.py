"""Multiple-testing correction and walk-forward validation."""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd
from scipy.stats import norm

from tradebot.engine import Config, backtest, metrics

EULER = 0.5772156649


def expected_max_z(n_trials: int) -> float:
    """Expected maximum of n_trials standard normals (Bailey & Lopez de Prado)."""
    if n_trials < 2:
        return 0.0
    return float(
        (1 - EULER) * norm.ppf(1 - 1 / n_trials)
        + EULER * norm.ppf(1 - 1 / (n_trials * np.e))
    )


def deflated_sharpe(sharpe: float, n_trials: int, n_obs: int,
                    skew: float = 0.0, kurtosis: float = 3.0,
                    periods_per_year: int = 365,
                    trials_sharpe_std: float | None = None) -> dict:
    """
    sharpe: annualized Sharpe of your BEST strategy
    n_trials: how many variations you tested. Be honest.
    n_obs: number of return observations behind that Sharpe
    trials_sharpe_std: annualized std of the Sharpes across all trials,
        if you have it. Without it we use the std of the Sharpe estimator
        under the null (true Sharpe = 0), which is 1/sqrt(n_obs - 1).

    The formula works on per-period (not annualized) Sharpe ratios, so
    we de-annualize first. Mixing an annualized Sharpe with the number of
    daily observations would make almost anything pass.
    """
    if n_obs < 3:
        raise ValueError('n_obs must be at least 3')
    if n_trials < 1:
        raise ValueError('n_trials must be at least 1')

    scale = np.sqrt(periods_per_year)
    sr = sharpe / scale
    if trials_sharpe_std is not None:
        sr_std = trials_sharpe_std / scale
    else:
        sr_std = 1 / np.sqrt(n_obs - 1)

    # expected max Sharpe from n_trials of pure noise
    sr0 = sr_std * expected_max_z(n_trials)

    denom = np.sqrt(max(
        1 - skew * sr + ((kurtosis - 1) / 4) * sr**2, 1e-12
    ))
    dsr = norm.cdf((sr - sr0) * np.sqrt(n_obs - 1) / denom)

    return {
        'deflated_sharpe': round(float(dsr), 3),
        'expected_max_from_noise': round(float(sr0 * scale), 2),
        'verdict': 'PASS' if dsr > 0.95 else 'REJECT',
    }


StrategyFn = Callable[[pd.Series], dict]


def walk_forward(prices: pd.Series, strategy_fn: StrategyFn, cfg: Config,
                 train_days: int = 180, test_days: int = 60,
                 min_obs: int = 20) -> dict:
    """
    strategy_fn(train_prices) -> {'signal_fn': f, 'params': {...}}
    signal_fn(history) -> signal series; the value at t may only use
    history up to and including t (the critic's leakage test checks this).

    Optimize on the past, trade the future, roll forward.
    """
    results = []
    oos = []
    i = 0
    while i + train_days + test_days <= len(prices):
        train = prices.iloc[i: i + train_days]
        end = i + train_days + test_days

        fitted = strategy_fn(train)          # fit on train only
        # Indicators need warm-up, so the signal sees the history up to the
        # end of the test window. One extra bar before the test window lets
        # the first test bar trade on the last train-bar signal.
        history = prices.iloc[:end]
        signal = fitted['signal_fn'](history)
        window = prices.iloc[i + train_days - 1: end]
        bt = backtest(window, signal.reindex(window.index), cfg).iloc[1:]

        results.append({
            'start': bt.index[0],
            'params': fitted.get('params', {}),
            **metrics(bt['net'], cfg, min_obs=min_obs),
        })
        oos.append(bt['net'])
        i += test_days

    if not results:
        raise ValueError(
            f'need at least {train_days + test_days} bars, got {len(prices)}'
        )

    df = pd.DataFrame(results)
    sharpe = df['sharpe'] if 'sharpe' in df else pd.Series(dtype=float)
    return {
        'folds': df,
        'oos_net': pd.concat(oos),
        'mean_sharpe': round(float(sharpe.mean()), 2),
        'positive_folds': f"{int((sharpe > 0).sum())}/{len(df)}",
        'positive_ratio': float((sharpe > 0).mean()) if len(df) else 0.0,
        'worst_fold': round(float(sharpe.min()), 2),
    }
