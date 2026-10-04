"""Re-test of the "hold-favourite" baseline from livetennisapi/polymarket-tennis.

That repository is an observe-only data toolkit; its only strategies are the
arena baselines. hold-favourite: buy the pre-match favourite (higher-priced
outcome) once and hold to the end. (fade-first-break needs game-by-game
scores from the vendor's keyed API; there is no public history to replay it.)

Pre-registered BEFORE any result was seen (+1 trial -> 57):
  universe   resolved singles moneyline markets (Gamma tag "tennis", sportsMarketType
             "moneyline", match slug, not doubles) with a game start in the last 30 days
  entry      price of each outcome = last Polymarket price-history point (hourly) at or
             before gameStartTime, at most 6 h old; favourite = the outcome above 0.50
  cost       theirs (arena rules): the price, no fee, no slippage. Realistic: price +
             half-spread 0.005 + the market's own taker fee (feeSchedule; none -> 0)
  stake      10 USD per match (the arena's STAKE_USD); returns = daily P&L / 1000 USD
  settle     the market's own resolution (1 / 0); markets resolved otherwise are skipped
  gates      deflated Sharpe > 0.95 with 57 trials; walk-forward 7-day folds >= 60 %
             positive, mean > 0. No look-ahead: only prices before the match starts.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from tradebot.engine import Config, metrics
from tradebot.stats import deflated_sharpe

GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
N_TRIALS = 57
FOLD_DAYS = 7
STAKE = 10.0
BANKROLL = 1000.0
HALF_SPREAD = 0.005
MAX_AGE = 6 * 3600
MATCH_SLUG = re.compile(r'^(?:atp|wta|itf|challenger|ch)-.+-\d{4}-\d{2}-\d{2}$')


def _json(v):
    return json.loads(v) if isinstance(v, str) else v


def parse_market(e: dict) -> dict | None:
    slug = str(e.get('slug') or '').lower()
    if not MATCH_SLUG.match(slug) or '-doubles-' in slug:
        return None
    for m in e.get('markets') or []:
        if m.get('sportsMarketType') != 'moneyline':
            continue
        try:
            outs, prices, toks = _json(m['outcomes']), _json(m['outcomePrices']), _json(m['clobTokenIds'])
            start = datetime.fromisoformat(str(m['gameStartTime']).replace(' ', 'T').replace('+00', '+00:00'))
        except (KeyError, TypeError, ValueError):
            return None
        if len(outs) != 2 or len(toks) != 2 or [str(p) for p in prices] not in (['1', '0'], ['0', '1']):
            return None
        sched = m.get('feeSchedule') or {}
        return {'slug': slug, 'start': int(start.timestamp()), 'outcomes': outs, 'token0': str(toks[0]),
                'winner': 0 if str(prices[0]) == '1' else 1,
                'fee_rate': float(sched.get('rate', 0) or 0), 'fee_exp': float(sched.get('exponent', 1) or 1)}
    return None


def decide(mk: dict, hist: list) -> dict | None:
    before = [(t, p) for t, p in hist if t <= mk['start'] and mk['start'] - t <= MAX_AGE]
    if not before:
        return None
    p0 = before[-1][1]
    if p0 == 0.5:
        return None
    fav = 0 if p0 > 0.5 else 1
    price = p0 if fav == 0 else 1 - p0
    paid = min(0.99, price + HALF_SPREAD)
    cost = paid + mk['fee_rate'] * (paid * (1 - paid)) ** mk['fee_exp']
    won = fav == mk['winner']
    return {'slug': mk['slug'], 'day': datetime.fromtimestamp(mk['start'], timezone.utc).date(),
            'favourite': mk['outcomes'][fav], 'price': round(price, 4), 'cost': round(cost, 4), 'won': won,
            'pnl_theirs': STAKE * ((1 / price if won else 0) - 1),
            'pnl_real': STAKE * ((1 / cost if won else 0) - 1)}


def evaluate(trades: list[dict], n_markets: int) -> dict:
    out = {'markets': n_markets, 'n_trades': len(trades)}
    if not trades:
        out['verdict'] = 'NO TRADES'
        return out
    t = pd.DataFrame(trades)
    out.update({'win_rate': round(float(t['won'].mean()), 4), 'avg_price': round(float(t['price'].mean()), 4),
                'breakeven_win_rate_real': round(float(t['cost'].mean()), 4),
                'pnl_theirs_usd': round(float(t['pnl_theirs'].sum()), 2),
                'pnl_real_usd': round(float(t['pnl_real'].sum()), 2),
                'staked_usd': round(STAKE * len(t), 2)})
    bands = pd.cut(t['price'], [0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
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
    out['folds'] = {'n': len(fs), 'positive': f'{sum(x > 0 for x in fs)}/{len(fs)}',
                    'pnl_usd': [round(x * BANKROLL, 2) for x in fs]}
    dsr = deflated_sharpe(m['sharpe'], N_TRIALS, m['n_obs'], m['skew'], m['kurtosis'], 365)
    out['dsr'] = dsr
    gates = {'no_leakage': True, 'deflated_sharpe': bool(dsr['deflated_sharpe'] > 0.95),
             'walk_forward': bool(sum(x > 0 for x in fs) / len(fs) >= 0.6 and np.mean(fs) > 0)}
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
            if r.status_code == 422:
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(2 ** i)
    return None


def collect(days: int = 30, workers: int = 8, log=print):
    http = requests.Session()
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    mks, seen, old_pages = [], set(), 0
    for page in range(150):
        batch = _get(http, f'{GAMMA}/events', {'tag_slug': 'tennis', 'closed': 'true', 'limit': 100,
                                               'offset': page * 100, 'order': 'endDate', 'ascending': 'false'})
        if not batch:
            break
        parsed = [parse_market(e) for e in batch]
        for mk in parsed:
            if mk and mk['start'] >= since and mk['slug'] not in seen:
                seen.add(mk['slug'])
                mks.append(mk)
        # stop after 3 pages in a row with no match inside the window (a single old
        # futures event in a page must not end the scan early)
        recent = [mk for mk in parsed if mk and mk['start'] >= since]
        old_pages = 0 if recent else old_pages + 1
        if len(batch) < 100 or old_pages >= 3:
            break
    log(f'{len(mks)} resolved singles moneyline markets since {datetime.fromtimestamp(since, timezone.utc).date()}, '
        f'fee rates {sorted({m["fee_rate"] for m in mks})}')

    def hist(mk):
        h = _get(http, f'{CLOB}/prices-history', {'market': mk['token0'], 'interval': 'max', 'fidelity': 60})
        return [(int(x['t']), float(x['p'])) for x in (h or {}).get('history', [])]
    with ThreadPoolExecutor(workers) as pool:
        hists = list(pool.map(hist, mks))
    log(f'price history for {sum(1 for h in hists if h)}/{len(mks)} markets')
    return mks, hists


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='tennisbot.hold_favourite')
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    mks, hists = collect(a.days)
    trades = [d for d in (decide(m, h) for m, h in zip(mks, hists)) if d]
    res = evaluate(trades, len(mks))
    print(json.dumps(res, indent=2, default=str))
    if a.out:
        with open(a.out, 'w') as f:
            json.dump({**res, 'trades': trades}, f, indent=1, default=str)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
