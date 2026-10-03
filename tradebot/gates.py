"""Three hard gates before any strategy touches real money.

1. The critic finds no leakage
2. The deflated Sharpe clears the multiple-testing bar
3. Out-of-sample performance survives walk-forward

Skip any gate and you've built a very fast way to lose money.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from tradebot import engine
from tradebot.critic import leakage_test, review
from tradebot.engine import Config, metrics
from tradebot.stats import deflated_sharpe, walk_forward

# The critic must clear these items in code we can read.
BLOCKING_ITEMS = (1, 3, 4, 8)


@dataclass
class GateThresholds:
    min_positive_folds: float = 0.6  # share of walk-forward folds with Sharpe > 0
    min_worst_fold: float = -2.0     # worst single fold Sharpe
    min_dsr: float = 0.95


def run_gates(prices: pd.Series, fit_fn: Callable[[pd.Series], dict],
              cfg: Config, n_trials: int, train_days: int = 180,
              test_days: int = 60, sources: tuple = (),
              thresholds: GateThresholds | None = None) -> dict:
    """
    fit_fn(train_prices) -> {'signal_fn': f, 'params': {...}}
    n_trials: every variation you tried while researching this idea.
    sources: extra modules/functions whose code the critic should read
             (the engine is always included).
    """
    th = thresholds or GateThresholds()

    # Gate 1: critic
    first = fit_fn(prices.iloc[:train_days])
    leak = leakage_test(first['signal_fn'], prices)
    code = '\n'.join(inspect.getsource(obj) for obj in (engine, *sources))
    items = review(code, prices=prices, params_fitted_out_of_sample=True)
    blocking = [it for it in items
                if it['item'] in BLOCKING_ITEMS and it['status'] == 'PRESENT']
    gate1 = leak['status'] == 'ABSENT' and not blocking

    # Gate 3 runs before gate 2: the DSR is computed on out-of-sample returns.
    wf = walk_forward(prices, fit_fn, cfg, train_days, test_days)
    gate3 = (
        wf['positive_ratio'] >= th.min_positive_folds
        and wf['worst_fold'] >= th.min_worst_fold
        and wf['mean_sharpe'] > 0
    )

    # Gate 2: deflated Sharpe on the stitched out-of-sample returns
    oos = metrics(wf['oos_net'], cfg, min_obs=30)
    if 'error' in oos:
        dsr = {'deflated_sharpe': 0.0, 'expected_max_from_noise': None,
               'verdict': 'REJECT'}
    else:
        dsr = deflated_sharpe(oos['sharpe'], n_trials, oos['n_obs'],
                              oos['skew'], oos['kurtosis'],
                              cfg.periods_per_year)
    gate2 = dsr['deflated_sharpe'] > th.min_dsr

    return {
        'gate1_no_leakage': gate1,
        'gate2_deflated_sharpe': gate2,
        'gate3_walk_forward': gate3,
        'approved': gate1 and gate2 and gate3,
        'leakage': leak,
        'critic': items,
        'blocking': blocking,
        'walk_forward': wf,
        'oos_metrics': oos,
        'dsr': dsr,
    }
