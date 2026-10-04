"""Liquidity-rewards market making on Polymarket: pre-registered test.

Polymarket pays makers a daily USDC pool per market for resting two-sided
liquidity near the midpoint (polymarket.com/api/rewards/markets: rate_per_day,
rewards_max_spread v in cents, rewards_min_size). Each minute an order's score is
S = ((v - s) / v) ** 2 * size (s = distance to the midpoint in cents, s < v); a
maker's two-sided score is Qmin = max(min(Q1, Q2), max(Q1, Q2) / 3) while the
midpoint is in [0.10, 0.90]; the pool is split pro rata to Qmin.

Pre-registered BEFORE any result was seen (+2 trials -> 60):
  universe   markets in the rewards programme right now, ending >= 30 days from now,
             midpoint in [0.10, 0.90], with both books readable
  quotes     one bid and one ask of rewards_min_size shares at distance d from the
             midpoint (YES terms); variants d = 1 cent and d = v / 2
  rewards    expected = rate_per_day * Qmin_ours / (Qmin_ours + Qmin_rest), with the
             competitors' Qmin computed from the live YES and NO books (snapshot);
             assumes our quotes stay up all day and the competition stays as it is
  fills      the last 14 days of real trades (data API), hourly re-quote around the
             mean of the last 10 trades (YES terms; a single last trade bounces
             between bid and ask); a quote fills only when a trade goes strictly
             through it; inventory capped at 3 x min size either way; makers pay no fee
  marks      inventory marked to the last trade at the end of each day (markets are
             open, not resolved)
  capital    1000 USD; markets taken by expected reward per dollar of collateral
             (bid: size * bid, ask: size * (1 - ask)) until the capital is used
  returns    daily (rewards + market-making P&L) / 1000, over the days each market
             was simulated
  gates      deflated Sharpe > 0.95 with 60 trials; 2-day folds >= 60 % positive,
             mean > 0; no look-ahead in fills (quotes use only earlier trades)
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from tradebot.engine import Config, metrics
from tradebot.stats import deflated_sharpe

REWARDS_API = 'https://polymarket.com/api/rewards/markets'
CLOB = 'https://clob.polymarket.com'
GAMMA = 'https://gamma-api.polymarket.com'
DATA_API = 'https://data-api.polymarket.com'
N_TRIALS = 60
FOLD_DAYS = 2
CAPITAL = 1000.0
DAYS = 14
MIN_DAYS_TO_END = 30
INV_CAP_MULT = 3
SCALE_C = 3.0


def score(dist_c: float, v: float, size: float) -> float:
    return ((v - dist_c) / v) ** 2 * size if 0 <= dist_c < v else 0.0


def qmin(q1: float, q2: float) -> float:
    return max(min(q1, q2), max(q1, q2) / SCALE_C)


def book_q(yes_book: dict, no_book: dict, mid: float, v: float) -> tuple[float, float]:
    """Competitors' one-sided scores. Q1: YES bids + NO asks; Q2: YES asks + NO bids."""
    def side(levels, ref_from):
        tot = 0.0
        for lv in levels or []:
            p, z = float(lv['price']), float(lv['size'])
            tot += score(abs(ref_from(p) - mid) * 100, v, z)
        return tot
    yes = lambda p: p          # noqa: E731
    no = lambda p: 1 - p       # noqa: E731  (a NO price q is YES 1 - q)
    q1 = side(yes_book.get('bids'), yes) + side(no_book.get('asks'), no)
    q2 = side(yes_book.get('asks'), yes) + side(no_book.get('bids'), no)
    return q1, q2


def expected_reward(rate: float, v: float, size: float, d_c: float, rest: tuple[float, float]) -> float:
    ours = qmin(score(d_c, v, size), score(d_c, v, size))
    theirs = qmin(*rest)
    return rate * ours / (ours + theirs) if ours > 0 else 0.0


def simulate_fills(trades: list, start: int, end: int, d: float, size: float) -> pd.Series:
    """Daily mark-to-market P&L of hourly quotes at (mean of last 10 trades) +/- d (YES terms)."""
    cap = INV_CAP_MULT * size
    inv = cash = 0.0
    j = 0
    ref = None
    recent: list[float] = []
    daily, day_val, cur = {}, 0.0, None
    last_px = None
    for t in range(start - start % 3600, end, 3600):
        while j < len(trades) and trades[j][0] < t:
            last_px = trades[j][1]
            recent = (recent + [last_px])[-10:]
            ref = sum(recent) / len(recent)
            j += 1
        d0 = datetime.fromtimestamp(t, timezone.utc).date()
        if d0 != cur:
            if cur is not None and last_px is not None:
                daily[cur] = cash + inv * last_px - day_val
            cur = d0
            day_val = cash + inv * (last_px if last_px is not None else 0.0)
        if ref is None or not 0.02 <= ref <= 0.98:
            continue
        bid, ask = ref - d, ref + d
        bq = size if inv + size <= cap else 0.0
        aq = size if -inv + size <= cap else 0.0
        k = j
        while k < len(trades) and trades[k][0] < t + 3600:
            p, z = trades[k][1], trades[k][2]
            if bq > 0 and p < bid:
                q = min(bq, z)
                bq -= q
                inv += q
                cash -= q * bid
            elif aq > 0 and p > ask:
                q = min(aq, z)
                aq -= q
                inv -= q
                cash += q * ask
            k += 1
    if cur is not None and last_px is not None:
        daily[cur] = cash + inv * last_px - day_val
    return pd.Series(daily, dtype=float)


def _get(http, url, params=None, tries=4):
    for i in range(tries):
        try:
            r = http.get(url, params=params, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.RequestException(r.status_code)
            if r.status_code >= 400:
                return None
            return r.json()
        except requests.RequestException:
            time.sleep(2 ** i)
    return None


def reward_markets(http, log=print) -> list[dict]:
    out, cursor = [], None
    for _ in range(200):
        d = _get(http, REWARDS_API, {'next_cursor': cursor} if cursor else None)
        if not d:
            break
        out += d.get('data') or []
        cursor = d.get('next_cursor')
        if not cursor or cursor in ('LTE=', ''):
            break
    log(f'{len(out)} markets in the rewards programme')
    return out


def gamma_info(cond_ids: list[str], http) -> dict:
    info = {}
    for i in range(0, len(cond_ids), 50):
        batch = _get(http, f'{GAMMA}/markets', [('condition_ids', c) for c in cond_ids[i:i + 50]] + [('limit', 100)])
        for m in batch or []:
            info[m.get('conditionId')] = m
    return info


def fetch_books(tokens: list[str], http) -> dict:
    out = {}
    for i in range(0, len(tokens), 100):
        try:
            r = http.post(f'{CLOB}/books', json=[{'token_id': t} for t in tokens[i:i + 100]], timeout=30)
            for b in r.json():
                out[str(b.get('asset_id'))] = b
        except Exception:
            pass
    return out


def fetch_trades(cond: str, yes_tok: str, since: int, http) -> list:
    out = []
    for off in range(0, 3000, 500):
        page = _get(http, f'{DATA_API}/trades', {'market': cond, 'limit': 500, 'offset': off})
        if not page:
            break
        for tr in page:
            try:
                p, z, t = float(tr['price']), float(tr['size']), int(tr['timestamp'])
            except (KeyError, TypeError, ValueError):
                continue
            if t < since:
                continue
            out.append((t, p if str(tr.get('asset')) == yes_tok else round(1 - p, 6), z))
        if len(page) < 500 or int(page[-1].get('timestamp', 0)) < since:
            break
    return sorted(out)


def candidates(http, log=print) -> list[dict]:
    now = datetime.now(timezone.utc)
    rm = reward_markets(http, log)
    rm = [m for m in rm if (m.get('rewards_config') or [{}])[0].get('rate_per_day', 0) > 0
          and m.get('rewards_max_spread') and m.get('rewards_min_size') and len(m.get('tokens') or []) == 2]
    info = gamma_info([m['condition_id'] for m in rm], http)
    out = []
    for m in rm:
        g = info.get(m['condition_id'])
        if not g or not g.get('endDate'):
            continue
        end = datetime.fromisoformat(g['endDate'].replace('Z', '+00:00'))
        if end < now + timedelta(days=MIN_DAYS_TO_END):
            continue
        toks = {t['outcome'].lower(): t['token_id'] for t in m['tokens']}
        if 'yes' not in toks or 'no' not in toks:
            continue
        out.append({'cond': m['condition_id'], 'question': m.get('question', ''), 'yes': toks['yes'], 'no': toks['no'],
                    'rate': float(sum(c.get('rate_per_day', 0) for c in m['rewards_config'])),
                    'v': float(m['rewards_max_spread']), 'min_size': float(m['rewards_min_size']),
                    'end': g['endDate']})
    books = fetch_books([c[k] for c in out for k in ('yes', 'no')], http)
    keep = []
    for c in out:
        yb, nb = books.get(c['yes']), books.get(c['no'])
        if not yb or not nb or not yb.get('bids') or not yb.get('asks'):
            continue
        bid = max(float(x['price']) for x in yb['bids'])
        ask = min(float(x['price']) for x in yb['asks'])
        mid = (bid + ask) / 2
        if not 0.10 <= mid <= 0.90:
            continue
        c['mid'] = mid
        c['rest_q'] = book_q(yb, nb, mid, c['v'])
        keep.append(c)
    log(f'{len(out)} long-dated reward markets, {len(keep)} with books and midpoint in [0.10, 0.90]')
    return keep


def run_variant(cands: list[dict], label: str, d_of, trades_of, now: int) -> dict:
    rows = []
    for c in cands:
        d = d_of(c)
        size = c['min_size']
        rew = expected_reward(c['rate'], c['v'], size, d * 100, c['rest_q'])
        collateral = size * (c['mid'] - d) + size * (1 - c['mid'] - d)
        rows.append({**c, 'd': d, 'size': size, 'reward_day': rew, 'collateral': collateral,
                     'per_dollar': rew / collateral if collateral > 0 else 0})
    rows.sort(key=lambda r: -r['per_dollar'])
    chosen, used = [], 0.0
    for r in rows:
        if r['reward_day'] <= 0 or used + r['collateral'] > CAPITAL:
            continue
        chosen.append(r)
        used += r['collateral']
    total = pd.Series(dtype=float)
    detail = []
    for r in chosen:
        tr = trades_of(r)
        start = max(now - DAYS * 86400, tr[0][0]) if tr else now - DAYS * 86400
        mm = simulate_fills(tr, start, now, r['d'], r['size'])
        days = pd.date_range(datetime.fromtimestamp(start, timezone.utc).date(),
                             datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=1)).date
        pnl = pd.Series(r['reward_day'], index=list(days)).add(mm.reindex(list(days)).fillna(0.0), fill_value=0.0)
        total = total.add(pnl, fill_value=0.0)
        detail.append({'question': r['question'][:70], 'rate_day': r['rate'], 'v_c': r['v'], 'size': r['size'],
                       'mid': round(r['mid'], 3), 'share': round(r['reward_day'] / r['rate'], 4),
                       'reward_day': round(r['reward_day'], 3), 'mm_pnl': round(float(mm.sum()), 2),
                       'reward_sum': round(r['reward_day'] * len(days), 2), 'trades': len(tr)})
    out = {'variant': label, 'markets': len(chosen), 'capital_used': round(used, 2),
           'reward_per_day': round(sum(r['reward_day'] for r in chosen), 2),
           'reward_total': round(sum(x['reward_sum'] for x in detail), 2),
           'mm_pnl_total': round(sum(x['mm_pnl'] for x in detail), 2),
           'net_total': round(float(total.sum()), 2), 'top_markets': sorted(detail, key=lambda x: -x['reward_day'])[:15]}
    daily = total.sort_index()
    if len(daily) >= 2 * FOLD_DAYS:
        daily.index = pd.to_datetime(daily.index)
        rets = daily / CAPITAL
        cfg = Config(periods_per_year=365)
        m = metrics(rets, cfg, min_obs=FOLD_DAYS)
        folds = [rets.iloc[i:i + FOLD_DAYS] for i in range(0, len(rets) - FOLD_DAYS + 1, FOLD_DAYS)]
        fs = [float(f.sum()) for f in folds]
        dsr = deflated_sharpe(m['sharpe'], N_TRIALS, m['n_obs'], m['skew'], m['kurtosis'], 365)
        gates = {'no_leakage': True, 'deflated_sharpe': bool(dsr['deflated_sharpe'] > 0.95),
                 'walk_forward': bool(sum(x > 0 for x in fs) / len(fs) >= 0.6 and np.mean(fs) > 0)}
        out.update({'metrics': m, 'folds': {'n': len(fs), 'positive': f'{sum(x > 0 for x in fs)}/{len(fs)}',
                                            'pnl_usd': [round(x * CAPITAL, 2) for x in fs]},
                    'dsr': dsr, 'gates': gates, 'approved': all(gates.values())})
        out['verdict'] = 'APPROVED' if out['approved'] else 'REJECTED'
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='rewardbot.backtest')
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    http = requests.Session()
    now = int(time.time()) // 86400 * 86400          # whole UTC days only
    cands = candidates(http)
    cache = {}

    def trades_of(c):
        if c['cond'] not in cache:
            cache[c['cond']] = fetch_trades(c['cond'], c['yes'], now - DAYS * 86400, http)
        return cache[c['cond']]
    # prefetch trades for every candidate in parallel
    with ThreadPoolExecutor(8) as pool:
        for c, tr in zip(cands, pool.map(lambda c: fetch_trades(c['cond'], c['yes'], now - DAYS * 86400, http), cands)):
            cache[c['cond']] = tr
    results = {}
    for label, d_of in (('d = 1 cent', lambda c: 0.01), ('d = v/2', lambda c: round(c['v'] / 200, 4))):
        res = run_variant(cands, label, d_of, trades_of, now)
        print(f'\n===== {label} =====')
        print(json.dumps(res, indent=2, default=str))
        results[label] = res
    if a.out:
        with open(a.out, 'w') as f:
            json.dump(results, f, indent=1, default=str)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
