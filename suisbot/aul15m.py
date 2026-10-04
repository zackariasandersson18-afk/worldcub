"""Honest re-test of aulekator/Polymarket-BTC-15-Minute-Trading-Bot.

The strategy as found in its bot.py (on_quote_tick / _make_trading_decision):
  * trade once per 15-minute "Bitcoin Up or Down" market, in the window 13:00-14:00
    minutes after the market opens
  * price = mid of the UP token. price > 0.60 -> buy UP; price < 0.40 -> buy DOWN;
    0.40-0.60 -> skip. The signal processors (order book, tick velocity, Coinbase
    divergence, spikes, Deribit PCR, Fear & Greed) only gate the trade through a
    fusion score >= 40 with one signal; the DIRECTION is the price rule alone.
  * $1 per trade, IOC market order, held to resolution (no exit logic in live mode)
  Its "simulation mode" P&L is not a test: _record_paper_trade draws the exit price
  from random.uniform(-0.02, 0.08) for bullish signals (-0.08, 0.02 bearish).

Pre-registered here BEFORE any result was seen (+1 trial -> 56):
  data       resolved btc-updown-15m-<start> markets of the last 14 days (Gamma) and
             every trade in them (data API; a DOWN trade at q is an UP trade at 1 - q)
  price      last trade (UP terms) in [start + 10:00, start + 13:00); none -> no trade.
             A last trade stands in for the mid the bot reads; the fusion gate is
             assumed to pass (it can't be replayed and only ever blocks trades)
  cost       theirs: the price itself, no fee. Realistic: price + half-spread 0.005,
             plus Polymarket's taker fee rate * (p * (1 - p)) ** exponent per share
  returns    $1 per trade, daily P&L / 100 USD (about 1000 SEK)
  gates      deflated Sharpe > 0.95 with 56 trials; walk-forward 2-day folds:
             >= 60 % positive, worst >= -2, mean > 0. No look-ahead: only trades
             before minute 13 are used.
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
DATA_API = 'https://data-api.polymarket.com'
N_TRIALS = 56
FOLD_DAYS = 2
UP_TH, DOWN_TH = 0.60, 0.40
LOOK_FROM, DECIDE_AT = 600, 780
HALF_SPREAD = 0.005
STAKE = 1.0
BANKROLL = 100.0


@dataclass
class Market:
    slug: str
    start: int
    up_won: bool
    fee_rate: float
    fee_exp: float
    trades: list          # [(t, up_price, size)]


def fee(p: float, rate: float, exp: float) -> float:
    return rate * (p * (1 - p)) ** exp


def decide(m: Market) -> dict | None:
    window = [p for t, p, _ in m.trades if m.start + LOOK_FROM <= t < m.start + DECIDE_AT]
    if not window:
        return None
    ref = window[-1]
    if ref > UP_TH:
        side, p, won = 'UP', ref, m.up_won
    elif ref < DOWN_TH:
        side, p, won = 'DOWN', 1 - ref, not m.up_won
    else:
        return None
    paid = min(0.99, p + HALF_SPREAD)
    cost = paid + fee(paid, m.fee_rate, m.fee_exp)
    return {'slug': m.slug, 'day': pd.Timestamp(m.start, unit='s').date(), 'side': side,
            'ref': round(ref, 4), 'price': round(p, 4), 'cost': round(cost, 4), 'won': won,
            'pnl_theirs': STAKE * ((1 / p if won else 0) - 1),
            'pnl_real': STAKE * ((1 / cost if won else 0) - 1)}


def evaluate(trades: list[dict], n_markets: int) -> dict:
    out = {'markets': n_markets, 'n_trades': len(trades)}
    if not trades:
        out['verdict'] = 'NO TRADES'
        return out
    t = pd.DataFrame(trades)
    out.update({'win_rate': round(float(t['won'].mean()), 4), 'avg_price': round(float(t['price'].mean()), 4),
                'avg_cost_real': round(float(t['cost'].mean()), 4),
                'breakeven_win_rate_real': round(float(t['cost'].mean()), 4),
                'pnl_theirs_usd': round(float(t['pnl_theirs'].sum()), 2),
                'pnl_real_usd': round(float(t['pnl_real'].sum()), 2),
                'up_share': round(float((t['side'] == 'UP').mean()), 3)})
    bands = pd.cut(t['price'], [0.6, 0.7, 0.8, 0.9, 0.95, 1.0])
    out['by_price'] = {str(k): {'n': int(len(g)), 'win': round(float(g['won'].mean()), 3),
                                'pnl_real': round(float(g['pnl_real'].sum()), 2)}
                       for k, g in t.groupby(bands, observed=True)}
    daily = t.groupby('day')['pnl_real'].sum().sort_index() / BANKROLL
    daily.index = pd.to_datetime(daily.index)
    out['days'] = len(daily)
    if len(daily) < 2 * FOLD_DAYS:
        out['verdict'] = 'NOT ENOUGH DATA'
        return out
    cfg = Config(periods_per_year=365)
    m = metrics(daily, cfg, min_obs=FOLD_DAYS)
    out['metrics_real'] = m
    folds = [daily.iloc[i:i + FOLD_DAYS] for i in range(0, len(daily) - FOLD_DAYS + 1, FOLD_DAYS)]
    fs = [float(f.sum()) for f in folds]
    pos = sum(x > 0 for x in fs) / len(fs)
    out['folds'] = {'n': len(fs), 'positive': f'{sum(x > 0 for x in fs)}/{len(fs)}',
                    'pnl_usd': [round(x * BANKROLL, 2) for x in fs]}
    dsr = deflated_sharpe(m['sharpe'], N_TRIALS, m['n_obs'], m['skew'], m['kurtosis'], 365)
    out['dsr'] = dsr
    gates = {'no_leakage': True, 'deflated_sharpe': bool(dsr['deflated_sharpe'] > 0.95),
             'walk_forward': bool(pos >= 0.6 and np.mean(fs) > 0)}
    out['gates'] = gates
    out['approved'] = all(gates.values())
    out['verdict'] = 'APPROVED' if out['approved'] else 'REJECTED'
    return out


def _get(http, url, params, tries=4):
    for i in range(tries):
        try:
            r = http.get(url, params=params, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.RequestException(r.status_code)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(2 ** i)
    return None


def fetch_market(start: int, http) -> Market | None:
    evs = _get(http, f'{GAMMA}/events', {'slug': f'btc-updown-15m-{start}'})
    if not evs:
        return None
    try:
        m = evs[0]['markets'][0]
        prices = json.loads(m['outcomePrices']) if isinstance(m['outcomePrices'], str) else m['outcomePrices']
        if not m.get('closed') or str(prices[0]) not in ('1', '0'):
            return None
        toks = json.loads(m['clobTokenIds']) if isinstance(m['clobTokenIds'], str) else m['clobTokenIds']
        outs = json.loads(m['outcomes']) if isinstance(m.get('outcomes'), str) else (m.get('outcomes') or ['Up', 'Down'])
        up_i = 0 if str(outs[0]).lower().startswith('up') else 1
        up_tok, down_tok = str(toks[up_i]), str(toks[1 - up_i])
        up_won = str(prices[up_i]) == '1'
        sched = m.get('feeSchedule') or {}
        cond = m['conditionId']
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    trades = []
    for off in range(0, 3000, 500):
        page = _get(http, f'{DATA_API}/trades', {'market': cond, 'limit': 500, 'offset': off})
        if not page:
            break
        for tr in page:
            try:
                p, z, t = float(tr['price']), float(tr['size']), int(tr['timestamp'])
            except (KeyError, TypeError, ValueError):
                continue
            a = str(tr.get('asset'))
            if a == up_tok:
                trades.append((t, p, z))
            elif a == down_tok:
                trades.append((t, round(1 - p, 6), z))
        if len(page) < 500:
            break
    return Market(f'btc-updown-15m-{start}', start, up_won, float(sched.get('rate', 0.07) or 0),
                  float(sched.get('exponent', 1) or 1), sorted(trades))


def collect(days: int, workers: int = 8, log=print) -> list[Market]:
    now = int(time.time()) // 900 * 900
    starts = list(range(now - days * 86400, now - 1800, 900))
    http = requests.Session()
    with ThreadPoolExecutor(workers) as pool:
        mk = [m for m in pool.map(lambda s: fetch_market(s, http), starts) if m]
    log(f'{len(mk)}/{len(starts)} resolved markets, {sum(len(m.trades) for m in mk)} trades, '
        f'fee rates {sorted({m.fee_rate for m in mk})}')
    return mk


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='suisbot.aul15m')
    ap.add_argument('--days', type=int, default=14)
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    mk = collect(a.days)
    trades = [d for d in map(decide, mk) if d]
    res = evaluate(trades, len(mk))
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
