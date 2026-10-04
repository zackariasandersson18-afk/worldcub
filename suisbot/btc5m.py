"""Their BTC 5-minute Up/Down strategy, reproduced and backtested.

Reproduced from backend/core/signals.py, backend/data/crypto.py and
backend/config.py of suislanchez/polymarket-kalshi-weather-bot:
  * 60 one-minute candles -> RSI(14, Wilder), momentum 1/5/15 m, VWAP(30)
    deviation, SMA5-SMA15, plus a contrarian "market skew" term
  * composite with weights 0.20/0.35/0.20/0.15/0.10 -> P(up) = 0.5 + 0.15c,
    clipped to [0.35, 0.65]; trade the side with the larger edge if
    edge >= 2 %, >= 2 of 4 indicators agree and entry price <= 0.55
  * size: 15 % Kelly, at most 5 % of bankroll and $75; $300 daily loss limit
  * they scan every 60 s and trade a window once it is <= 1800 s from its end

Backtest: decision 1800 s before each window's end, using only candles closed
before it and the last market price at or before it. Two accountings:
  * theirs:   fill at the market price, no fees (what their bot records)
  * realistic: + half a cent of spread + Polymarket taker fee
               rate * (p * (1 - p)) ** exponent (crypto 5 m: 0.07, 1)
Only the realistic one goes through the gates (49 trials counted).
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests

from tradebot.engine import Config, metrics
from tradebot.stats import deflated_sharpe

GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
KLINES = ('https://data-api.binance.vision/api/v3/klines', 'https://api.binance.com/api/v3/klines')

# their settings (backend/config.py)
W_RSI, W_MOM, W_VWAP, W_SMA, W_SKEW = 0.20, 0.35, 0.20, 0.15, 0.10
MIN_EDGE, MAX_ENTRY = 0.02, 0.55
KELLY, MAX_FRACTION, MAX_TRADE, DAILY_LOSS_LIMIT = 0.15, 0.05, 75.0, 300.0
BANKROLL = 10_000.0
DECISION_BEFORE_END = 1800
HALF_SPREAD = 0.005
N_TRIALS = 49
FOLD_DAYS = 2


# ------------------------------------------------------------------ their indicators
def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    d = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ag = sum(x for x in d[:period] if x > 0) / period
    al = sum(-x for x in d[:period] if x < 0) / period
    for x in d[period:]:
        ag = (ag * (period - 1) + max(x, 0.0)) / period
        al = (al * (period - 1) + max(-x, 0.0)) / period
    return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)


def microstructure(candles: np.ndarray) -> dict:
    """candles: rows [open, high, low, close, volume], oldest first (60 rows)."""
    highs, lows, closes, vols = candles[:, 1], candles[:, 2], candles[:, 3], candles[:, 4]
    c = closes.tolist()
    price = c[-1]

    def pct(lb):
        return (c[-1] - c[-1 - lb]) / c[-1 - lb] * 100 if len(c) > lb and c[-1 - lb] > 0 else 0.0
    n = min(30, len(c))
    tp = (highs[-n:] + lows[-n:] + closes[-n:]) / 3
    v = vols[-n:]
    vwap = float((tp * v).sum() / v.sum()) if v.sum() > 0 else price
    sma5 = sum(c[-5:]) / 5
    sma15 = sum(c[-15:]) / 15
    return {'rsi': rsi(c), 'm1': pct(1), 'm5': pct(5), 'm15': pct(15),
            'vwap_dev': (price - vwap) / vwap * 100, 'sma': (sma5 - sma15) / price * 100}


def signal(m: dict, up_price: float) -> dict:
    """Their generate_btc_signal, line by line."""
    r = m['rsi']
    if r < 30:
        rs = 0.5 + (30 - r) / 30
    elif r > 70:
        rs = -0.5 - (r - 70) / 30
    elif r < 45:
        rs = (45 - r) / 30
    elif r > 55:
        rs = -(r - 55) / 30
    else:
        rs = 0.0
    rs = max(-1.0, min(1.0, rs))
    mom = max(-1.0, min(1.0, (m['m1'] * 0.5 + m['m5'] * 0.35 + m['m15'] * 0.15) / 0.10))
    vw = max(-1.0, min(1.0, m['vwap_dev'] / 0.05))
    sm = max(-1.0, min(1.0, m['sma'] / 0.03))
    skew = max(-1.0, min(1.0, -(up_price - 0.50) * 4))
    ups = sum(s > 0.05 for s in (rs, mom, vw, sm))
    downs = sum(s < -0.05 for s in (rs, mom, vw, sm))
    comp = rs * W_RSI + mom * W_MOM + vw * W_VWAP + sm * W_SMA + skew * W_SKEW
    p_up = max(0.35, min(0.65, 0.50 + comp * 0.15))
    up_edge, down_edge = p_up - up_price, (1 - p_up) - (1 - up_price)
    side = 'UP' if up_edge >= down_edge else 'DOWN'
    edge = max(up_edge, down_edge)
    entry = up_price if side == 'UP' else 1 - up_price
    ok = (ups >= 2 or downs >= 2) and entry <= MAX_ENTRY and edge >= MIN_EDGE
    win_p = p_up if side == 'UP' else 1 - p_up
    return {'p_up': p_up, 'side': side, 'edge': edge, 'entry': entry, 'win_p': win_p, 'ok': ok}


def kelly_size(win_p: float, price: float, bankroll: float = BANKROLL) -> float:
    """Their calculate_kelly_size."""
    if not 0 < price < 1:
        return 0.0
    odds = (1 - price) / price
    k = (win_p * odds - (1 - win_p)) / odds * KELLY
    return min(max(min(k, MAX_FRACTION), 0.0) * bankroll, MAX_TRADE)


# ------------------------------------------------------------------ data
@dataclass
class Window:
    slug: str
    start: int
    end: int
    up_won: bool
    up_token: str
    fee_rate: float
    fee_exp: float


def parse_window(e: dict) -> Window | None:
    try:
        m = e['markets'][0]
        prices = json.loads(m['outcomePrices']) if isinstance(m['outcomePrices'], str) else m['outcomePrices']
        if not m.get('closed') or prices[0] not in ('1', '0', 1, 0):
            return None
        end = int(pd.Timestamp(m['endDate']).timestamp())
        start = int(pd.Timestamp(m.get('eventStartTime') or e.get('startTime')).timestamp())
        sched = m.get('feeSchedule') or {}
        return Window(e['slug'], start, end, str(prices[0]) == '1', json.loads(m['clobTokenIds'])[0],
                      float(sched.get('rate', 0.07)), float(sched.get('exponent', 1)))
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def _get(http, url, params, tries=4):
    for i in range(tries):
        try:
            r = http.get(url, params=params, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(response=r)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2 ** i)


def fetch_windows(starts: list[int], http, workers=8, log=print) -> list[tuple[Window, list]]:
    """Window metadata + UP-token price history for each 5-minute window start."""
    def one(ts):
        try:
            evs = _get(http, f'{GAMMA}/events', {'slug': f'btc-updown-5m-{ts}'})
            w = parse_window(evs[0]) if evs else None
            if not w:
                return None
            hist = _get(http, f'{CLOB}/prices-history', {'market': w.up_token, 'interval': 'max', 'fidelity': 1})
            return w, [(int(p['t']), float(p['p'])) for p in hist.get('history', [])]
        except Exception:
            return None
    with ThreadPoolExecutor(workers) as pool:
        out = [x for x in pool.map(one, starts) if x]
    log(f'{len(out)}/{len(starts)} windows with metadata')
    return out


def fetch_candles(start_ms: int, end_ms: int, http) -> pd.DataFrame:
    rows, t = [], start_ms
    while t < end_ms:
        for url in KLINES:
            try:
                batch = _get(http, url, {'symbol': 'BTCUSDT', 'interval': '1m', 'startTime': t,
                                         'endTime': end_ms, 'limit': 1000})
                break
            except Exception:
                batch = None
        if not batch:
            break
        rows += batch
        t = batch[-1][0] + 60_000
    df = pd.DataFrame([[r[0] // 1000, *map(float, r[1:6])] for r in rows],
                      columns=['t', 'open', 'high', 'low', 'close', 'volume']).drop_duplicates('t')
    return df.set_index('t').sort_index()


def price_at(hist: list[tuple[int, float]], ts: int, max_age: int = 3 * 3600) -> float | None:
    before = [p for t, p in hist if t <= ts and ts - t <= max_age]
    return before[-1] if before else None


# ------------------------------------------------------------------ backtest
def run(windows: list[tuple[Window, list]], candles: pd.DataFrame) -> dict:
    trades = []
    loss_today: dict[str, float] = {}
    for w, hist in sorted(windows, key=lambda x: x[0].start):
        decision = w.end - DECISION_BEFORE_END
        up_price = price_at(hist, decision)
        if up_price is None or not 0.02 < up_price < 0.98:
            continue
        # 60 one-minute candles that had closed by the decision time
        last_open = (decision // 60) * 60 - 60
        c = candles.loc[last_open - 59 * 60:last_open]
        if len(c) < 60:
            continue
        sig = signal(microstructure(c[['open', 'high', 'low', 'close', 'volume']].to_numpy()), up_price)
        if not sig['ok']:
            continue
        day = pd.Timestamp(w.end, unit='s').strftime('%Y-%m-%d')
        if loss_today.get(day, 0.0) >= DAILY_LOSS_LIMIT:
            continue  # their circuit breaker
        stake = kelly_size(sig['win_p'], sig['entry'])
        if stake < 1:
            continue
        won = w.up_won if sig['side'] == 'UP' else not w.up_won
        p = sig['entry']
        cost_real = p + HALF_SPREAD + w.fee_rate * (p * (1 - p)) ** w.fee_exp
        pnl_theirs = stake / p * won - stake
        pnl_real = stake / cost_real * won - stake
        loss_today[day] = loss_today.get(day, 0.0) + max(0.0, -pnl_theirs)
        trades.append({'slug': w.slug, 'day': day, 'side': sig['side'], 'p_up': round(sig['p_up'], 3),
                       'price': p, 'edge': round(sig['edge'], 3), 'stake': round(stake, 2), 'won': won,
                       'pnl_theirs': round(pnl_theirs, 2), 'pnl_real': round(pnl_real, 2),
                       'cost_real': round(cost_real, 4)})
    return evaluate(trades)


def evaluate(trades: list[dict]) -> dict:
    out = {'n_trades': len(trades), 'trades': trades}
    if len(trades) < 30:
        out['verdict'] = 'NOT ENOUGH DATA'
        return out
    t = pd.DataFrame(trades)
    out.update({
        'win_rate': round(float(t['won'].mean()), 4),
        'avg_price': round(float(t['price'].mean()), 4),
        'breakeven_win_rate_real': round(float(t['cost_real'].mean()), 4),
        'avg_claimed_edge': round(float(t['edge'].mean()), 4),
        'pnl_theirs_usd': round(float(t['pnl_theirs'].sum()), 2),
        'pnl_real_usd': round(float(t['pnl_real'].sum()), 2),
        'days': int(t['day'].nunique()),
    })
    daily = t.groupby('day')['pnl_real'].sum() / BANKROLL
    daily.index = pd.to_datetime(daily.index)
    cfg = Config(periods_per_year=365)
    m = metrics(daily, cfg, min_obs=FOLD_DAYS)
    out['metrics_real'] = m
    if len(daily) < 2 * FOLD_DAYS or 'error' in m:
        out['verdict'] = 'NOT ENOUGH DATA'
        return out
    folds = [daily.iloc[i:i + FOLD_DAYS] for i in range(0, len(daily) - FOLD_DAYS + 1, FOLD_DAYS)]
    fs = [float(f.sum()) for f in folds]  # fold PnL (2 days are too short for a Sharpe)
    out['folds'] = {'n': len(fs), 'positive': f'{sum(x > 0 for x in fs)}/{len(fs)}'}
    dsr = deflated_sharpe(m['sharpe'], N_TRIALS, m['n_obs'], m['skew'], m['kurtosis'], 365)
    out['dsr'] = dsr
    gates = {'no_leakage': True,  # candles closed before, price at or before the decision
             'deflated_sharpe': dsr['deflated_sharpe'] > 0.95,
             'walk_forward': sum(x > 0 for x in fs) / len(fs) >= 0.6 and float(np.mean(fs)) > 0}
    out['gates'] = gates
    out['approved'] = all(gates.values())
    out['verdict'] = 'APPROVED' if out['approved'] else 'REJECTED'
    return out


def collect(days: int, http=None, log=print):
    http = http or requests.Session()
    now = int(time.time()) // 300 * 300
    starts = list(range(now - days * 86400, now - 3600, 300))
    windows = fetch_windows(starts, http, log=log)
    with_hist = [x for x in windows if x[1]]
    log(f'{len(with_hist)} windows have price history')
    if not with_hist:
        return [], pd.DataFrame()
    lo = min(w.start for w, _ in with_hist) - 4 * 3600
    hi = max(w.end for w, _ in with_hist)
    candles = fetch_candles(lo * 1000, hi * 1000, http)
    log(f'{len(candles)} one-minute candles')
    return with_hist, candles


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='suisbot.btc5m')
    ap.add_argument('--days', type=int, default=14)
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    windows, candles = collect(a.days)
    res = run(windows, candles)
    trades = res.pop('trades')
    print(json.dumps(res, indent=2, default=str))
    if trades:
        t = pd.DataFrame(trades)
        print('\nby side:\n', t.groupby('side')[['won', 'pnl_theirs', 'pnl_real']].agg(['count', 'mean', 'sum']).round(3))
        print('\nby day:\n', t.groupby('day')[['pnl_theirs', 'pnl_real']].sum().round(2))
    if a.out:
        with open(a.out, 'w') as f:
            json.dump({**res, 'trades': trades}, f, indent=1, default=str)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
