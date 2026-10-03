"""Backtest engine and metrics.

If this is wrong, everything downstream is wrong. Build it once, build it
correctly, never rewrite it.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Config:
    initial_capital: float = 10_000
    fee_bps: float = 5.0        # exchange fee per side, charged on every unit of turnover
    slippage_bps: float = 3.0   # crypto spreads are wider than you think
    max_leverage: float = 1.0
    periods_per_year: int = 365 # crypto trades 24/7


def backtest(prices: pd.Series, signal: pd.Series, cfg: Config) -> pd.DataFrame:
    """
    prices: close prices, datetime index
    signal: target position, -1 to +1, computed from data
            available AT that timestamp
    """
    signal = signal.reindex(prices.index)

    # THE MOST IMPORTANT LINE IN THIS PACKAGE.
    # You act on the NEXT bar, not the one you just saw.
    position = signal.shift(1).fillna(0).clip(
        -cfg.max_leverage, cfg.max_leverage
    )

    # Simple (not log) returns: position * log-return is wrong for shorts
    # and leverage, simple returns compound correctly for both.
    returns = prices.pct_change().fillna(0)
    gross = position * returns

    turnover = position.diff().abs().fillna(position.abs())
    costs = turnover * (cfg.fee_bps + cfg.slippage_bps) / 1e4

    net = gross - costs
    equity = cfg.initial_capital * (1 + net).cumprod()

    return pd.DataFrame({
        'position': position,
        'gross': gross,
        'costs': costs,
        'net': net,
        'equity': equity,
    })


def metrics(net: pd.Series, cfg: Config, min_obs: int = 100) -> dict:
    r = net.dropna()
    if len(r) < min_obs:
        return {'error': 'insufficient_data', 'n_obs': len(r)}

    ann_return = r.mean() * cfg.periods_per_year
    ann_vol = r.std() * np.sqrt(cfg.periods_per_year)
    sharpe = ann_return / ann_vol if ann_vol > 0 else 0.0

    equity = (1 + r).cumprod()
    peak = equity.cummax().clip(lower=1.0)  # starting equity counts as a peak
    dd = (equity - peak) / peak
    max_dd = dd.min()

    # how long you sat underwater -- the number that
    # actually decides whether you'd have held on
    underwater = (dd < 0).astype(int)
    longest_dd = underwater.groupby(
        (underwater != underwater.shift()).cumsum()
    ).sum().max()

    return {
        'sharpe': round(float(sharpe), 2),
        'ann_return': round(float(ann_return) * 100, 1),
        'max_drawdown': round(float(max_dd) * 100, 1),
        'longest_dd_periods': int(longest_dd),
        'calmar': round(float(ann_return / abs(max_dd)), 2) if max_dd else 0.0,
        'skew': round(float(r.skew()), 3) if len(r) > 2 else 0.0,
        'kurtosis': round(float(r.kurt()) + 3.0, 3) if len(r) > 3 else 3.0,
        'n_obs': len(r),
        'red_flag': sharpe > 2,  # above 2 on daily crypto: leakage until proven otherwise
    }
