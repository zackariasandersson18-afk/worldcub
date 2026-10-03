"""Regime split: bull, bear and chop using a 200-period moving average."""
from __future__ import annotations

import pandas as pd

from tradebot.engine import Config, metrics


def label_regimes(prices: pd.Series, ma_period: int = 200,
                  chop_band: float = 0.02) -> pd.Series:
    """bull: close more than chop_band above the MA, bear: more than
    chop_band below, chop: in between. Uses only past and current data."""
    ma = prices.rolling(ma_period).mean()
    dist = prices / ma - 1
    labels = pd.Series('chop', index=prices.index)
    labels[dist > chop_band] = 'bull'
    labels[dist < -chop_band] = 'bear'
    labels[ma.isna()] = 'warmup'
    return labels


def regime_report(prices: pd.Series, net: pd.Series, cfg: Config,
                  ma_period: int = 200, min_obs: int = 20) -> dict:
    labels = label_regimes(prices, ma_period).reindex(net.index)
    per_regime = {}
    for regime in ('bull', 'bear', 'chop'):
        r = net[labels == regime]
        per_regime[regime] = metrics(r, cfg, min_obs=min_obs)

    present = [k for k, v in per_regime.items() if 'error' not in v]
    positive = [k for k in present if per_regime[k]['sharpe'] > 0]
    if len(positive) == 1:
        verdict = f'edge exists only in the {positive[0]} regime'
    elif not positive:
        verdict = 'no regime has a positive Sharpe'
    else:
        verdict = f'positive Sharpe in: {", ".join(positive)}'

    return {
        'regimes': per_regime,
        'has_bull_and_bear': 'bull' in present and 'bear' in present,
        'verdict': verdict,
    }
