"""Re-test of the "smart quoter" in bigmacman1129/crypto-ai-trading-bot.

That bot (Node.js, a fork of the ADAMANT tradebot) has three parts:
  * smart quoter: Avellaneda-Stoikov style two-sided quotes (trade/engines/quoteEngine.js)
  * arbitrage scanner across small exchanges (no public order-book history: not testable)
  * "classic wash-volume market-making" (trading with itself to fake volume: market
    manipulation, deliberately NOT tested or reproduced)

The quoter, ported 1:1 with its default config (config.default.jsonc "smartMm"/"risk"):
  every 4 s: mid history (40 samples) -> sigma = std of log returns
  invNorm = clip(inventory / maxInventory, -1, 1); gamma_eff = 0.15 * 0.6
  reservation = mid * (1 - gamma_eff * invNorm * max(sigma, 1e-4))
  half = clip(mid * sigma, mid * 0.15% / 2, mid * 2.5% / 2); skew = invNorm * half * 0.85
  bid = reservation - half - skew, ask = reservation + half - skew
  requote only when bid or ask moved >= 0.08 % of mid
  sizes: 100 USDT per side (risk.maxOrderQuote); maxInventory = 50 % of the 50/50 target
  (250 USDT of a 1000 USDT book); open notional capped at 250 USDT; daily loss halt 50 USDT

Pre-registered here BEFORE any result was seen (+1 trial -> 58):
  data       Binance spot 1-second klines, BTCUSDT and DOGEUSDT, last 14 full days
  fills      a resting bid fills (at its price) when a following 1 s bar trades strictly
             below it; an ask when one trades strictly above. Touching is not a fill.
  costs      theirs: no fee. Realistic: 0.10 % maker fee per fill (Binance standard; the
             bot's own exchanges charge 0.1-0.2 %)
  returns    each pair runs a 1000 USDT book; daily P&L marked to the day's last price,
             summed over both pairs / 2000
  gates      deflated Sharpe > 0.95 with 58 trials; 2-day folds >= 60 % positive, mean > 0
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

from tradebot.engine import Config, metrics
from tradebot.stats import deflated_sharpe

N_TRIALS = 58
FOLD_DAYS = 2
STEP = 4                 # seconds between quoter iterations
LOOKBACK = 40
MIN_SPREAD, MAX_SPREAD = 0.15, 2.5
GAMMA = 0.15 * 0.6
REQUOTE = 0.08
ORDER_QUOTE = 100.0
BOOK = 1000.0
MAX_INV_QUOTE = 250.0
MAX_NOTIONAL = 250.0
DAILY_LOSS = 50.0
MAKER_FEE = 0.001
SYMBOLS = ('BTCUSDT', 'DOGEUSDT')


def compute_quotes(mid: float, sigma: float, inv_norm: float) -> tuple[float, float]:
    res = mid * (1 - GAMMA * inv_norm * max(sigma, 1e-4))
    half = min(mid * MAX_SPREAD / 200, max(mid * MIN_SPREAD / 200, mid * sigma))
    skew = inv_norm * half * 0.85
    return res - half - skew, res + half - skew


def realized_vol(mids: list[float]) -> float:
    if len(mids) < 3:
        return 0.0
    r = np.diff(np.log(np.asarray(mids)))
    return float(np.std(r, ddof=1)) if len(r) >= 2 else 0.0


def simulate(bars: pd.DataFrame, fee: float) -> pd.Series:
    """bars: 1-second rows with columns t (unix s), high, low, close. Returns daily P&L (USDT)."""
    t, hi, lo, cl = (bars[c].to_numpy() for c in ('t', 'high', 'low', 'close'))
    inv = cash = 0.0                  # inventory (base, vs the 50/50 target) and cash flow
    mids, quotes = [], None
    bid = ask = None
    bq = aq = 0.0
    day_pnl, day_start_value, cur_day, halted = {}, 0.0, None, False
    for i in range(0, len(t) - STEP, STEP):
        mid = cl[i]
        d = pd.Timestamp(t[i], unit='s').date()
        value = cash + inv * mid
        if d != cur_day:
            if cur_day is not None:
                day_pnl[cur_day] = value - day_start_value
            cur_day, day_start_value, halted = d, value, False
        if value - day_start_value <= -DAILY_LOSS:
            halted = True
        mids.append(mid)
        if len(mids) > LOOKBACK:
            mids.pop(0)
        if halted:
            bid = ask = None
            continue
        sigma = realized_vol(mids)
        max_inv = MAX_INV_QUOTE / mid
        nb, na = compute_quotes(mid, sigma, max(-1.0, min(1.0, inv / max_inv)))
        if quotes is None or abs(nb - quotes[0]) / mid * 100 >= REQUOTE or abs(na - quotes[1]) / mid * 100 >= REQUOTE:
            quotes = (nb, na)
            bid, ask = nb, na
            size = ORDER_QUOTE / mid
            bq = size if (inv + size) * mid <= MAX_NOTIONAL else 0.0
            aq = size if (-inv + size) * mid <= MAX_NOTIONAL else 0.0
        # fills over the next STEP seconds (strictly through our price)
        seg_lo, seg_hi = lo[i + 1:i + 1 + STEP].min(), hi[i + 1:i + 1 + STEP].max()
        if bid is not None and bq > 0 and seg_lo < bid:
            inv += bq
            cash -= bq * bid * (1 + fee)
            bq = 0.0
        if ask is not None and aq > 0 and seg_hi > ask:
            inv -= aq
            cash += aq * ask * (1 - fee)
            aq = 0.0
    if cur_day is not None:
        day_pnl[cur_day] = cash + inv * cl[-1] - day_start_value
    return pd.Series(day_pnl)


def fetch_day(symbol: str, day: date, http) -> pd.DataFrame | None:
    url = f'https://data.binance.vision/data/spot/daily/klines/{symbol}/1s/{symbol}-1s-{day.isoformat()}.zip'
    r = http.get(url, timeout=120)
    if r.status_code != 200:
        return None
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        raw = pd.read_csv(z.open(z.namelist()[0]), header=None, usecols=[0, 2, 3, 4])
    raw.columns = ['open_time', 'high', 'low', 'close']
    ot = raw['open_time'].astype('int64')
    raw['t'] = np.where(ot > 1e14, ot // 1_000_000, ot // 1000)   # microseconds since 2025, else ms
    return raw[['t', 'high', 'low', 'close']]


def evaluate(daily: pd.Series, extra: dict) -> dict:
    out = dict(extra)
    daily = daily.sort_index()
    daily.index = pd.to_datetime(daily.index)
    rets = daily / (BOOK * len(SYMBOLS))
    cfg = Config(periods_per_year=365)
    m = metrics(rets, cfg, min_obs=FOLD_DAYS)
    out['metrics'] = m
    folds = [rets.iloc[i:i + FOLD_DAYS] for i in range(0, len(rets) - FOLD_DAYS + 1, FOLD_DAYS)]
    fs = [float(f.sum()) for f in folds]
    out['folds'] = {'n': len(fs), 'positive': f'{sum(x > 0 for x in fs)}/{len(fs)}',
                    'pnl_usdt': [round(x * BOOK * len(SYMBOLS), 2) for x in fs]}
    dsr = deflated_sharpe(m['sharpe'], N_TRIALS, m['n_obs'], m['skew'], m['kurtosis'], 365)
    out['dsr'] = dsr
    gates = {'no_leakage': True, 'deflated_sharpe': bool(dsr['deflated_sharpe'] > 0.95),
             'walk_forward': bool(sum(x > 0 for x in fs) / len(fs) >= 0.6 and np.mean(fs) > 0)}
    out['gates'] = gates
    out['approved'] = all(gates.values())
    out['verdict'] = 'APPROVED' if out['approved'] else 'REJECTED'
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='cryptobot.smart_quoter')
    ap.add_argument('--days', type=int, default=14)
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    http = requests.Session()
    days = [date.today() - timedelta(days=k) for k in range(a.days + 1, 1, -1)]
    results = {}
    for label, fee in (('theirs_no_fee', 0.0), ('realistic_0.10pct_maker', MAKER_FEE)):
        total, per = None, {}
        for sym in SYMBOLS:
            frames = [f for f in (fetch_day(sym, d, http) for d in days) if f is not None]
            bars = pd.concat(frames, ignore_index=True)
            pnl = simulate(bars, fee)
            per[sym] = {'days': len(pnl), 'pnl_usdt': round(float(pnl.sum()), 2),
                        'positive_days': f'{int((pnl > 0).sum())}/{len(pnl)}'}
            total = pnl if total is None else total.add(pnl, fill_value=0.0)
        res = evaluate(total, {'per_symbol': per, 'pnl_usdt': round(float(total.sum()), 2)})
        print(f'\n===== {label} =====')
        print(json.dumps(res, indent=2, default=str))
        results[label] = res
    if a.out:
        with open(a.out, 'w') as f:
            json.dump(results, f, indent=1, default=str)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
