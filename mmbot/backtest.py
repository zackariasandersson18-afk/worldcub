"""Market making on Polymarket temperature markets: walk-forward backtest.

Pre-registered BEFORE any result was seen (research round 6, +2 trials -> 53):

  universe   resolved "Highest temperature in <city>" events of the last 45 days;
             every bucket market with both tokens and a YES/NO resolution
  data       every trade from Polymarket's data API (price, size, time); a trade
             in the NO token at price q is a YES trade at 1 - q
  clock      hourly. At each hour t the bot cancels and re-posts:
               reference r = last trade (YES terms) strictly before t, at most
                             6 h old; no reference -> no quotes
               bid = r - h, ask = r + h (rounded to the 0.01 tick, away from r)
               only while 0.05 <= r <= 0.95 and t < the market's end date
               size 10 shares per side
  fills      during [t, t+1h) a trade at a YES price strictly below our bid fills
             our bid (at our bid) and one strictly above our ask fills our ask,
             each up to the trade's size and our remaining size. A trade exactly
             at our price is NOT a fill (queue position unknown).
  costs      makers pay no fee on Polymarket (rebates ignored). Selling YES not
             held = buying NO at 1 - ask.
  capital    1000 USD shared across all markets, simulated in time order: an
             order is only posted if the cash for it is free; positions per
             market are capped at 50 shares either way; payouts return at
             resolution (1 per YES share if YES won, 1 per NO share if NO won)
  variants   half-spread h = 0.02 and h = 0.04
  returns    PnL of each market booked on its event date, / 1000 USD
  gates      unchanged: no look-ahead (quotes use only earlier trades; unit
             tested), walk-forward 7-day folds (>= 60 % positive, worst >= -2,
             mean > 0) and deflated Sharpe > 0.95 with 53 trials
"""
from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import requests

from tradebot.engine import Config, metrics
from tradebot.stats import deflated_sharpe
from weatherbot import markets

N_TRIALS = 53
VARIANTS = (0.02, 0.04)
FOLD_DAYS = 7
SIZE = 10.0
MAX_POS = 50.0
CAPITAL = 1000.0
REF_MAX_AGE = 6 * 3600
DATA_API = 'https://data-api.polymarket.com'


@dataclass
class Market:
    key: str
    event_date: date
    city: str
    label: str
    end: int                      # unix: stop quoting
    yes_won: bool
    trades: list = field(default_factory=list)   # [(t, yes_price, size)] ascending


def quotes(ref: float | None, h: float) -> tuple[float | None, float | None]:
    if ref is None or not 0.05 <= ref <= 0.95:
        return None, None
    bid = math.floor(round((ref - h) * 100, 6)) / 100
    ask = math.ceil(round((ref + h) * 100, 6)) / 100
    return (bid if bid >= 0.01 else None), (ask if ask <= 0.99 else None)


def reference(trades: list, t: int, i: int) -> tuple[float | None, int]:
    """Last trade strictly before t (pointer i advanced past trades < t)."""
    while i < len(trades) and trades[i][0] < t:
        i += 1
    if i == 0:
        return None, i
    last_t, p, _ = trades[i - 1]
    return (p if t - last_t <= REF_MAX_AGE else None), i


SETTLE_DELAY = 24 * 3600   # payouts assumed back in the wallet a day after the market's end


def settle_payout(inv: float, yes_won: bool) -> float:
    """Long YES pays 1 per share if YES; a short (held as NO shares) pays 1 per share if NO."""
    if inv >= 0:
        return inv if yes_won else 0.0
    return 0.0 if yes_won else -inv


def simulate(mkts: list[Market], h: float, capital: float = CAPITAL) -> dict:
    """Real-wallet simulation in time order across all markets.

    Wallet flows: buy YES at bid -bid; sell held YES at ask +ask; sell YES not held =
    buy NO at 1 - ask -(1 - ask); buy YES against held NO = merge into 1 USD: -bid + 1;
    settlement pays 1 per winning share. Per-market PnL = sum of its flows."""
    wallet = capital
    st = {m.key: {'inv': 0.0, 'flow': 0.0, 'ptr': 0, 'fptr': 0, 'fills': 0, 'spread': 0.0} for m in mkts}
    live = [m for m in mkts if m.trades]
    if not live:
        return {'rows': [], 'min_wallet': wallet}
    start = min(m.trades[0][0] for m in live) // 3600 * 3600
    stop = max(m.end for m in live) + SETTLE_DELAY
    pending = sorted(live, key=lambda m: m.end)
    done, min_wallet = set(), wallet
    for t in range(start, stop + 3600, 3600):
        while pending and pending[0].end + SETTLE_DELAY <= t:
            m = pending.pop(0)
            pay = settle_payout(st[m.key]['inv'], m.yes_won)
            wallet += pay
            st[m.key]['flow'] += pay
            done.add(m.key)
        for m in live:
            if m.key in done or t >= m.end or t < m.trades[0][0] - 3600:
                continue
            s = st[m.key]
            ref, s['ptr'] = reference(m.trades, t, s['ptr'])
            bid, ask = quotes(ref, h)
            bq = max(0.0, min(SIZE, MAX_POS - s['inv'])) if bid else 0.0
            aq = max(0.0, min(SIZE, MAX_POS + s['inv'])) if ask else 0.0
            # cash reserved for the resting orders (worst case: fully filled)
            bcost = bq * bid if bq else 0.0
            if bcost > wallet:
                bq, bcost = 0.0, 0.0
            short = max(0.0, aq - max(s['inv'], 0.0))
            if short and short * (1 - ask) > wallet - bcost:
                aq -= short
            if bq <= 0 and aq <= 0:
                continue
            j = s['fptr']
            while j < len(m.trades) and m.trades[j][0] < t:
                j += 1
            while j < len(m.trades) and m.trades[j][0] < t + 3600:
                _, p, z = m.trades[j]
                j += 1
                if bq > 0 and p < bid:
                    q = min(bq, z)
                    bq -= q
                    cover = min(q, max(-s['inv'], 0.0))
                    flow = -q * bid + cover            # covered shares merge with NO into 1 USD
                    s['inv'] += q
                elif aq > 0 and p > ask:
                    q = min(aq, z)
                    aq -= q
                    sell = min(q, max(s['inv'], 0.0))
                    flow = sell * ask - (q - sell) * (1 - ask)
                    s['inv'] -= q
                else:
                    continue
                wallet += flow
                s['flow'] += flow
                s['fills'] += 1
                s['spread'] += q * abs((bid if p < bid else ask) - ref)
                min_wallet = min(min_wallet, wallet)
            s['fptr'] = j
    for m in pending:   # not reached in time order (shouldn't happen): settle now
        pay = settle_payout(st[m.key]['inv'], m.yes_won)
        st[m.key]['flow'] += pay
    rows = [{'key': m.key, 'date': m.event_date, 'city': m.city, 'bucket': m.label,
             'pnl': st[m.key]['flow'], 'fills': st[m.key]['fills'], 'spread': st[m.key]['spread']}
            for m in mkts]
    return {'rows': rows, 'min_wallet': min_wallet}


def evaluate(rows: list[dict], h: float) -> dict:
    df = pd.DataFrame(rows)
    traded = df[df['fills'] > 0]
    out = {'half_spread': h, 'markets': len(df), 'markets_traded': len(traded),
           'fills': int(df['fills'].sum()), 'pnl_usd': round(float(df['pnl'].sum()), 2),
           'spread_captured_usd': round(float(df['spread'].sum()), 2)}
    # adverse selection = what the spread earned minus what was actually kept
    out['adverse_selection_usd'] = round(out['spread_captured_usd'] - out['pnl_usd'], 2)
    daily = df.groupby('date')['pnl'].sum().sort_index() / CAPITAL
    daily.index = pd.to_datetime(daily.index)
    out['n_days'] = len(daily)
    if len(daily) < 2 * FOLD_DAYS:
        out['verdict'] = 'NOT ENOUGH DATA'
        return out
    cfg = Config(periods_per_year=365)
    m = metrics(daily, cfg, min_obs=FOLD_DAYS)
    out['metrics'] = m
    folds = [daily.iloc[i:i + FOLD_DAYS] for i in range(0, len(daily) - FOLD_DAYS + 1, FOLD_DAYS)]
    fs = [metrics(f, cfg, min_obs=FOLD_DAYS).get('sharpe', 0.0) for f in folds]
    pos = sum(s > 0 for s in fs) / len(fs)
    out['folds'] = {'n': len(fs), 'positive': f'{sum(s > 0 for s in fs)}/{len(fs)}',
                    'worst': round(min(fs), 2), 'mean': round(float(np.mean(fs)), 2),
                    'pnl_usd': [round(float(f.sum() * CAPITAL), 2) for f in folds]}
    dsr = deflated_sharpe(m['sharpe'], N_TRIALS, m['n_obs'], m['skew'], m['kurtosis'], 365)
    out['dsr'] = dsr
    gates = {'no_leakage': True,
             'deflated_sharpe': bool(dsr['deflated_sharpe'] > 0.95),
             'walk_forward': bool(pos >= 0.6 and min(fs) >= -2 and np.mean(fs) > 0)}
    out['gates'] = gates
    out['approved'] = all(gates.values())
    out['verdict'] = 'APPROVED' if out['approved'] else 'REJECTED'
    out['days_positive'] = f"{int((daily > 0).sum())}/{len(daily)}"
    out['best_day_usd'] = round(float(daily.max() * CAPITAL), 2)
    out['worst_day_usd'] = round(float(daily.min() * CAPITAL), 2)
    return out


# ------------------------------------------------------------------ data
def _get(http, url, params, tries=4):
    for k in range(tries):
        try:
            r = http.get(url, params=params, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.RequestException(r.status_code)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(2 ** k)
    return None


def fetch_trades(cond: str, yes_tok: str, no_tok: str, http) -> list:
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
            a = str(tr.get('asset'))
            if a == yes_tok:
                out.append((t, p, z))
            elif a == no_tok:
                out.append((t, round(1 - p, 6), z))
        if len(page) < 500:
            break
    return sorted(out)


def collect(days: int = 45, workers: int = 8, log=print) -> list[Market]:
    http = requests.Session()
    cutoff = date.today() - timedelta(days=days)
    raw = []
    for page in range(60):
        batch = _get(http, f'{markets.GAMMA}/events', {'tag_slug': markets.TAG, 'closed': 'true', 'limit': 100,
                                                       'offset': page * 100, 'order': 'endDate', 'ascending': 'false'})
        if not batch:
            break
        evs = [(e, markets.parse_event(e)) for e in batch]
        evs = [(e, ev) for e, ev in evs if ev and ev.winner]
        raw += [(e, ev) for e, ev in evs if ev.date >= cutoff]
        if len(batch) < 100 or (evs and max(ev.date for _, ev in evs) < cutoff):
            break
    specs = []
    for e, ev in raw:
        for m in e.get('markets') or []:
            toks = markets._json_list(m.get('clobTokenIds')) or []
            b = next((x for x in ev.buckets if x.label == m.get('groupItemTitle')), None)
            if len(toks) != 2 or not b or not m.get('conditionId') or not m.get('endDate'):
                continue
            end = int(datetime.fromisoformat(m['endDate'].replace('Z', '+00:00')).timestamp())
            specs.append((Market(f"{ev.id}:{b.label}", ev.date, ev.city, b.label, end, bool(b.resolved_yes)),
                          m['conditionId'], str(toks[0]), str(toks[1])))
    log(f'{len(raw)} resolved events since {cutoff}, {len(specs)} bucket markets')
    with ThreadPoolExecutor(workers) as pool:
        for (mk, *_), trades in zip(specs, pool.map(lambda s: fetch_trades(s[1], s[2], s[3], http), specs)):
            mk.trades = trades
    mk_list = [s[0] for s in specs]
    log(f"{sum(1 for m in mk_list if m.trades)} markets with trades, {sum(len(m.trades) for m in mk_list)} trades")
    return mk_list


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='mmbot.backtest')
    ap.add_argument('--days', type=int, default=45)
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    mk = collect(a.days)
    results = {}
    for h in VARIANTS:
        sim = simulate(mk, h)
        res = evaluate(sim['rows'], h)
        res['lowest_free_cash_usd'] = round(sim['min_wallet'], 2)
        print(f'\n===== half-spread {h} =====')
        print(json.dumps(res, indent=2, default=str))
        results[str(h)] = res
    if a.out:
        with open(a.out, 'w') as f:
            json.dump(results, f, indent=1, default=str)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
