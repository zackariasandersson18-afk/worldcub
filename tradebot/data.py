"""Price data: CSV, Binance public klines, and synthetic prices for demos.

All series are returned with a UTC index stamped at BAR CLOSE, so a
value at time t was known at time t.
"""
from __future__ import annotations

import os
import time

import numpy as np
import pandas as pd
import requests

# data-api.binance.vision serves public market data only and is reachable from
# regions (e.g. US cloud runners) where api.binance.com answers 451.
BINANCE_URLS = (
    'https://data-api.binance.vision/api/v3/klines',
    'https://api.binance.com/api/v3/klines',
)


def _klines_urls() -> tuple[str, ...]:
    custom = os.environ.get('TRADEBOT_KLINES_URL')
    return (custom,) if custom else BINANCE_URLS


def normalize(prices: pd.Series) -> pd.Series:
    """UTC index, sorted, no duplicates, no non-positive prices."""
    idx = pd.DatetimeIndex(prices.index)
    idx = idx.tz_localize('UTC') if idx.tz is None else idx.tz_convert('UTC')
    s = pd.Series(prices.values, index=idx, name='close').astype(float)
    s = s[~s.index.duplicated(keep='last')].sort_index().dropna()
    if (s <= 0).any():
        raise ValueError('prices must be positive')
    return s


def load_csv(path: str, time_col: str = 'timestamp',
             price_col: str = 'close') -> pd.Series:
    """Load close prices. Naive timestamps are assumed to be UTC bar closes."""
    df = pd.read_csv(path)
    for col in (time_col, price_col):
        if col not in df.columns:
            raise ValueError(f'column {col!r} missing, found {list(df.columns)}')
    ts = df[time_col]
    if pd.api.types.is_numeric_dtype(ts):
        unit = 'ms' if ts.max() > 1e11 else 's'
        idx = pd.to_datetime(ts, unit=unit, utc=True)
    else:
        idx = pd.to_datetime(ts, utc=True)
    return normalize(pd.Series(df[price_col].values, index=idx))


def check_alignment(*series: pd.Series) -> list[str]:
    """Problems that make series incomparable. Empty list = aligned."""
    problems = []
    for i, s in enumerate(series):
        tz = getattr(s.index, 'tz', None)
        if tz is None or str(tz) != 'UTC':
            problems.append(f'series {i}: index timezone is {tz}, expected UTC')
        if not s.index.is_monotonic_increasing:
            problems.append(f'series {i}: index not sorted')
        if s.index.has_duplicates:
            problems.append(f'series {i}: duplicate timestamps')
    freqs = {pd.infer_freq(s.index[:50]) for s in series if len(s) >= 3}
    if len(freqs) > 1:
        problems.append(f'series have different bar sizes: {sorted(map(str, freqs))}')
    return problems


def fetch_binance(symbol: str = 'BTCUSDT', interval: str = '1d',
                  start: str = '2019-01-01', end: str | None = None,
                  session: requests.Session | None = None) -> pd.Series:
    """Closed bars only, indexed by bar close time (UTC)."""
    http = session or requests.Session()
    start_ms = int(pd.Timestamp(start, tz='UTC').timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz='UTC').timestamp() * 1000) if end \
        else int(time.time() * 1000)

    def get(params):
        errors = []
        for url in _klines_urls():
            try:
                resp = http.get(url, params=params, timeout=20)
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                errors.append(f'{url}: {exc}')
        raise RuntimeError('all kline endpoints failed: ' + '; '.join(errors))

    rows = []
    while start_ms < end_ms:
        batch = get({
            'symbol': symbol, 'interval': interval,
            'startTime': start_ms, 'endTime': end_ms, 'limit': 1000,
        })
        if not batch:
            break
        rows.extend(batch)
        start_ms = batch[-1][6] + 1  # next bar after the last close time

    now_ms = int(time.time() * 1000)
    # kline: [open_time, open, high, low, close, volume, close_time, ...]
    closed = [k for k in rows if k[6] < now_ms]
    if not closed:
        raise ValueError(f'no closed bars for {symbol}')
    idx = pd.to_datetime([k[6] + 1 for k in closed], unit='ms', utc=True)
    return normalize(pd.Series([float(k[4]) for k in closed], index=idx))


def synthetic_prices(n: int = 1500, seed: int = 7, start: str = '2020-01-01',
                     drift: float = 0.0, vol: float = 0.035) -> pd.Series:
    """Random walk with alternating bull/bear drift. For demos and tests,
    NOT for drawing conclusions."""
    rng = np.random.default_rng(seed)
    regime_drift = np.where((np.arange(n) // 250) % 2 == 0, 0.002, -0.0015)
    r = rng.normal(drift + regime_drift, vol)
    prices = 10_000 * np.exp(np.cumsum(r))
    idx = pd.date_range(start, periods=n, freq='D', tz='UTC')
    return pd.Series(prices, index=idx, name='close')
