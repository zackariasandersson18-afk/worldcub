"""Production: a validated strategy is not a finished strategy. It decays.

Decide the kill condition before you deploy, while you're still objective.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def health_check(live_returns: pd.Series, backtest_metrics: dict,
                 window: int = 30, periods_per_year: int = 365) -> dict:
    """Run daily. Halt when it fires.

    live_returns: simple per-period returns of the live strategy
    backtest_metrics: output of engine.metrics (sharpe, max_drawdown in %)
    """
    live_returns = live_returns.dropna()
    if len(live_returns) < 2:
        return {'live_sharpe': 0.0, 'current_dd': 0.0, 'alerts': [],
                'action': 'CONTINUE', 'note': 'not enough live data'}

    recent = live_returns.tail(window)
    std = recent.std()
    live_sharpe = (
        recent.mean() / std * np.sqrt(periods_per_year) if std > 0 else 0.0
    )

    equity = (1 + live_returns).cumprod()
    peak = equity.cummax().iloc[-1]
    dd = (equity.iloc[-1] - peak) / peak

    alerts = []
    if live_sharpe < backtest_metrics['sharpe'] * 0.5:
        alerts.append('SHARPE_DECAY')
    if dd < backtest_metrics['max_drawdown'] / 100 * 1.5:
        alerts.append('DRAWDOWN_EXCEEDED')

    return {
        'live_sharpe': round(float(live_sharpe), 2),
        'current_dd': round(float(dd) * 100, 1),
        'alerts': alerts,
        'action': 'HALT' if alerts else 'CONTINUE',
    }
