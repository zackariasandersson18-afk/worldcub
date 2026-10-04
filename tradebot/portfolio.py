"""Multi-asset trend following (research round 2).

Pre-registered BEFORE looking at any result:

  universe   the ten largest non-stablecoin coins by market cap on 2019-01-01
             (not today's winners): BTC XRP ETH BCH EOS XLM LTC BSV TRX ADA,
             each against USDT on Binance. Coins Binance no longer lists
             (e.g. BSV, delisted April 2019) cannot be downloaded; they are
             reported as a survivorship gap, never silently dropped.
  signal     per coin: ensemble trend vote (share of 20/40/60/90/120-day
             lookbacks with a positive return) x volatility scaling to 40 %
             annualized, optionally x 200-day trend filter
  weights    equal share 1/N of the coins that have a price that day;
             long only, gross exposure <= 1
  variants   multi_ensemble_vol, multi_ensemble_regime_vol  (+2 trials)
  gates      unchanged (positive folds >= 60 %, worst fold >= -2, mean > 0,
             deflated Sharpe > 0.95)
"""
from __future__ import annotations

import inspect

import numpy as np
import pandas as pd

from tradebot import engine
from tradebot.critic import review
from tradebot.engine import Config, metrics
from tradebot.gates import BLOCKING_ITEMS, GateThresholds
from tradebot.stats import deflated_sharpe
from tradebot.strategies import ensemble_signal, regime_filter, vol_scale

UNIVERSE_2019 = {
    'BTC': ['BTCUSDT'], 'XRP': ['XRPUSDT'], 'ETH': ['ETHUSDT'],
    'BCH': ['BCHABCUSDT', 'BCHUSDT'], 'EOS': ['EOSUSDT'], 'XLM': ['XLMUSDT'],
    'LTC': ['LTCUSDT'], 'BSV': ['BCHSVUSDT', 'BSVUSDT'], 'TRX': ['TRXUSDT'],
    'ADA': ['ADAUSDT'],
}
VARIANTS = ('multi_ensemble_vol', 'multi_ensemble_regime_vol')


def load_universe(fetch, start: str = '2019-01-01', universe: dict = UNIVERSE_2019):
    """fetch(symbol, start) -> close series. Returns (prices DataFrame, report).
    A coin with several symbols (renamed pairs) is spliced, newest data winning."""
    cols, report = {}, {}
    for coin, symbols in universe.items():
        parts = []
        for sym in symbols:
            try:
                parts.append(fetch(sym, start))
            except Exception as exc:  # delisted / unknown symbol
                report.setdefault(coin, []).append(f'{sym}: {str(exc)[:80]}')
        if parts:
            s = pd.concat(parts).sort_index()
            cols[coin] = s[~s.index.duplicated(keep='last')]
    prices = pd.DataFrame(cols).sort_index()
    loaded = {c: (str(prices[c].first_valid_index())[:10], str(prices[c].last_valid_index())[:10])
              for c in prices}
    missing = [c for c in universe if c not in prices]
    return prices, {'loaded': loaded, 'missing': missing, 'errors': report}


def multi_signals(prices: pd.DataFrame, regime: bool) -> pd.DataFrame:
    """Target weights per coin, computed from data up to and including each day."""
    sig = prices.apply(ensemble_signal) * prices.apply(vol_scale)
    if regime:
        sig = sig * prices.apply(regime_filter)
    active = prices.notna().sum(axis=1).clip(lower=1)
    return sig.where(prices.notna(), 0.0).div(active, axis=0).fillna(0.0)


def variant_fn(name: str):
    if name == 'multi_ensemble_vol':
        return lambda p: multi_signals(p, regime=False)
    if name == 'multi_ensemble_regime_vol':
        return lambda p: multi_signals(p, regime=True)
    raise ValueError(f'unknown portfolio variant {name!r}')


def portfolio_backtest(prices: pd.DataFrame, signals: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Daily-rebalanced long-only portfolio; same conventions as engine.backtest."""
    signals = signals.reindex_like(prices).fillna(0.0)
    # act on the NEXT bar, never the one just seen
    positions = signals.shift(1).fillna(0.0)
    rets = prices.pct_change(fill_method=None).fillna(0.0)
    gross = (positions * rets).sum(axis=1)
    turnover = positions.diff().abs().fillna(positions.abs())
    costs = turnover.sum(axis=1) * (cfg.fee_bps + cfg.slippage_bps) / 1e4
    net = gross - costs
    return pd.DataFrame({'gross': gross, 'costs': costs, 'net': net,
                         'exposure': positions.sum(axis=1)})


def leakage_test_multi(signal_fn, prices: pd.DataFrame, n_cuts: int = 8, seed: int = 0) -> dict:
    """Weights at t must not change when any data after t changes."""
    rng = np.random.default_rng(seed)
    full = signal_fn(prices)
    n = len(prices)
    failures = []
    for cut in sorted(rng.integers(n // 3, n - 2, size=n_cuts)):
        truth = signal_fn(prices.iloc[:cut + 1]).fillna(0).to_numpy()
        scrambled = prices.copy()
        shock = np.exp(np.cumsum(rng.normal(0, 0.05, (n - cut - 1, prices.shape[1])), axis=0))
        scrambled.iloc[cut + 1:] = prices.iloc[cut].to_numpy() * shock
        scrambled = scrambled.where(prices.notna())
        for label, other in (('full history', full), ('scrambled future', signal_fn(scrambled))):
            seen = other.iloc[:cut + 1].fillna(0).to_numpy()
            bad = np.argwhere(~np.isclose(seen, truth))
            if bad.size:
                failures.append(f'weights at {prices.index[bad[0][0]]} differ using {label} '
                                f'after {prices.index[cut]}')
                break
    return {'status': 'PRESENT' if failures else 'ABSENT', 'cuts_tested': n_cuts,
            'failures': failures}


def folds(net: pd.Series, cfg: Config, train_days: int, test_days: int, min_obs: int = 20) -> dict:
    """No parameter is fitted, so walk-forward reduces to the same consecutive
    out-of-sample windows run_gates uses, starting after the training span."""
    oos = net.iloc[train_days:]
    rows = []
    for i in range(0, len(oos) - test_days + 1, test_days):
        chunk = oos.iloc[i:i + test_days]
        rows.append({'start': chunk.index[0], **metrics(chunk, cfg, min_obs=min_obs)})
    df = pd.DataFrame(rows)
    used = oos.iloc[:len(df) * test_days]
    return {'folds': df, 'oos_net': used,
            'mean_sharpe': round(float(df['sharpe'].mean()), 2),
            'positive_folds': f"{int((df['sharpe'] > 0).sum())}/{len(df)}",
            'positive_ratio': float((df['sharpe'] > 0).mean()),
            'worst_fold': round(float(df['sharpe'].min()), 2)}


def run_portfolio_gates(prices: pd.DataFrame, name: str, cfg: Config, n_trials: int,
                        train_days: int = 180, test_days: int = 60,
                        thresholds: GateThresholds | None = None) -> dict:
    th = thresholds or GateThresholds()
    fn = variant_fn(name)

    leak = leakage_test_multi(fn, prices)
    code = inspect.getsource(engine) + '\n' + inspect.getsource(portfolio_backtest)
    anchor = prices['BTC'] if 'BTC' in prices else prices.iloc[:, 0]
    items = review(code, prices=anchor.dropna(), params_fitted_out_of_sample=True)
    blocking = [it for it in items if it['item'] in BLOCKING_ITEMS and it['status'] == 'PRESENT']
    gate1 = leak['status'] == 'ABSENT' and not blocking

    bt = portfolio_backtest(prices, fn(prices), cfg)
    wf = folds(bt['net'], cfg, train_days, test_days)
    gate3 = (wf['positive_ratio'] >= th.min_positive_folds
             and wf['worst_fold'] >= th.min_worst_fold and wf['mean_sharpe'] > 0)

    oos = metrics(wf['oos_net'], cfg, min_obs=30)
    dsr = deflated_sharpe(oos['sharpe'], n_trials, oos['n_obs'], oos['skew'],
                          oos['kurtosis'], cfg.periods_per_year)
    gate2 = dsr['deflated_sharpe'] > th.min_dsr
    return {'gate1_no_leakage': gate1, 'gate2_deflated_sharpe': gate2,
            'gate3_walk_forward': gate3, 'approved': gate1 and gate2 and gate3,
            'leakage': leak, 'blocking': blocking, 'walk_forward': wf,
            'oos_metrics': oos, 'dsr': dsr,
            'avg_exposure': round(float(bt['exposure'].iloc[train_days:].mean()), 3)}
