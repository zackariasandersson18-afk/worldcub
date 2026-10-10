"""Liquidity rewards, version 2: rotating portfolio (virtual, read-only). Trial 62.

Version 1 (rewardbot.live) froze its 24 markets on day one; competitors moved in
(our share fell from ~30 % to ~13 %), five markets drifted out of the paid band and
two trending markets caused most of the market-making loss. Version 2 runs beside
it, on the same capital and quote rules, so the two can be compared.

Pre-registered BEFORE any result was seen (+1 trial -> 62):
  quotes     unchanged: rewards_min_size shares at mid +/- v/2 on both sides
  selection  every 24 h, from the live rewards programme with today's competition:
             midpoint in [0.15, 0.85] (margin inside the paid [0.10, 0.90] band),
             >= 30 days to resolution, and not trending: YES trades of the last 72 h
             span at most 10 cents; then the best estimated reward per dollar of
             collateral, greedily up to 1000 USD (version 1's rule)
  rebalance  a market that leaves the portfolio has its inventory closed at the
             current best bid (long) / best ask (short); kept markets keep theirs
  inventory  at most 1 x min size per market (version 1: 3 x)
  ledger     realistic only, the same R1-R5 rules as version 1's realistic ledger

Version 3 (trial 63, registered 2026-10-10 after version 2's first selection, which
estimated only 1.65 USD/day because the trend rule removed the two markets that pay;
no profit or loss of version 2 had been seen): identical to version 2 except that the
trend rule is off. Both versions refresh reward rates every tick (rewardbot.live.refresh_rates).

Version 4 (trial 65, registered 2026-10-10 before any version 4 result): starts with
version 3's selection rule, never does the full daily re-selection, and instead reviews
the portfolio every hour. A held market is swapped out only if
  (a) its share has fallen below half of its share at selection, or its midpoint is
      outside the paid band [0.10, 0.90], AND
  (b) a market not held offers at least 1.3 x its current reward per dollar of
      collateral (pot x current share / collateral), within the 1000 USD capital.
Swapped-out inventory is closed at the touch, as in a daily re-selection.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from rewardbot import backtest as bt
from rewardbot.live import add_history, fill_row, load, refresh_rates, save

VERSIONS = {'v2': {'trial': 62, 'max_range': 0.10, 'file': 'rewards_v2_state.json'},
            'v3': {'trial': 63, 'max_range': None, 'file': 'rewards_v3_state.json'},
            'v4': {'trial': 65, 'max_range': None, 'file': 'rewards_v4_state.json', 'review_s': 3600}}
REVIEW_SHARE_DROP = 0.5
REVIEW_BETTER = 1.3
CAPITAL = 1000.0
REBALANCE_S = 86400
MID_BAND = (0.15, 0.85)
TREND_LOOKBACK_S = 72 * 3600
INV_CAP_MULT = 1
SCREEN_TOP = 80          # trend check only for the best candidates (each needs a trades call)


def rank(http, log) -> list[dict]:
    """Every eligible market with its estimated reward per dollar of collateral, best first."""
    rows = []
    for c in bt.candidates(http, log):
        if not MID_BAND[0] <= c['mid'] <= MID_BAND[1]:
            continue
        d = round(c['v'] / 200, 4)
        rew = bt.expected_reward(c['rate'], c['v'], c['min_size'], d * 100, c['rest_q'])
        coll = c['min_size'] * (c['mid'] - d) + c['min_size'] * (1 - c['mid'] - d)
        if rew > 0 and coll > 0:
            rows.append({**c, 'd': d, 'est_reward_day': rew, 'collateral': coll, 'per_dollar': rew / coll})
    rows.sort(key=lambda r: -r['per_dollar'])
    return rows


KEEP = ('cond', 'question', 'yes', 'no', 'rate', 'v', 'min_size', 'd', 'collateral', 'est_reward_day', 'end')


def held(r: dict) -> dict:
    return {**{k: r[k] for k in KEEP}, 'share0': r['est_reward_day'] / r['rate'] if r['rate'] else 0.0}


def choose(http, now: float, log, max_range: float | None = 0.10) -> list[dict]:
    rows = rank(http, log)
    chosen, used, trending = [], 0.0, 0
    for r in rows[:SCREEN_TOP]:
        if used + r['collateral'] > CAPITAL:
            continue
        if max_range is not None:
            try:
                px = [p for _, p, _ in bt.fetch_trades(r['cond'], r['yes'], int(now - TREND_LOOKBACK_S), http)]
            except requests.RequestException:
                continue
            if px and max(px) - min(px) > max_range:
                trending += 1
                continue
        chosen.append(held(r))
        used += r['collateral']
    log(f'portfolio (max range {max_range}): {len(chosen)} markets, collateral {used:.2f} USD, {trending} trending skipped, '
        f'estimated {sum(c["est_reward_day"] for c in chosen):.2f}/day')
    return chosen


def review(st: dict, http, now: float, log) -> list[dict]:
    """Version 4's hourly review: swap out weak markets for clearly better ones (rules in the docstring)."""
    port, pos = st['portfolio'], st['positions']
    have = {c['cond'] for c in port}
    fresh = [r for r in rank(http, log) if r['cond'] not in have]
    used = sum(c['collateral'] for c in port)
    swaps = []
    def current(c):
        p = pos.get(c['cond']) or {}
        return c['rate'] * (p.get('share') or 0.0) / c['collateral'] if c['collateral'] else 0.0
    for c in sorted(port, key=current):
        p = pos.get(c['cond']) or {}
        share, mid = p.get('share'), p.get('mid')
        weak = (share is not None and share < REVIEW_SHARE_DROP * c.get('share0', share)) or \
               (mid is not None and not 0.10 <= mid <= 0.90)
        if not weak:
            continue
        cur = current(c)
        for i, r in enumerate(fresh):
            if r['per_dollar'] >= REVIEW_BETTER * cur and used - c['collateral'] + r['collateral'] <= CAPITAL:
                swaps.append((c, held(r)))
                used += r['collateral'] - c['collateral']
                fresh.pop(i)
                break
    if swaps:
        books = bt.fetch_books([c['yes'] for c, _ in swaps], http)
        for c, _ in swaps:
            if c['cond'] in pos:
                close_out(pos[c['cond']], books.get(c['yes']))
                pos[c['cond']]['share'] = None
        out = {c['cond'] for c, _ in swaps}
        st['portfolio'] = [c for c in port if c['cond'] not in out] + [n for _, n in swaps]
    st.setdefault('reviews', []).append({'ts': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'swapped': len(swaps),
                                         'out': [c['question'][:50] for c, _ in swaps], 'in': [n['question'][:50] for _, n in swaps]})
    st['reviews'] = st['reviews'][-500:]
    log(f'v4 review: {len(swaps)} swaps')
    return swaps


def close_out(pos: dict, yb: dict | None) -> None:
    """Close inventory at the touch: sell a long at the best bid, buy back a short at the best ask."""
    inv = pos['inv']
    if not inv:
        return
    bids = [float(x['price']) for x in (yb or {}).get('bids') or []]
    asks = [float(x['price']) for x in (yb or {}).get('asks') or []]
    px = (max(bids) if bids else 0.0) if inv > 0 else (min(asks) if asks else 1.0)
    pos['cash'] += inv * px
    pos['inv'] = 0.0
    pos['closed_at'] = px


def step(state_dir: str | Path, now: float | None = None, http=None, log=print, version: str = 'v2') -> dict:
    cfg = VERSIONS[version]
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    http = http or requests.Session()
    now = now or time.time()
    path = d / cfg['file']
    st = load(path, {})
    if not st:
        st = {'started': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'trial': cfg['trial'], 'version': version,
              'portfolio': [], 'positions': {}, 'history': [], 'fills': [], 'reward_total': 0.0,
              'last_ts': now, 'chosen_at': 0, 'rebalances': []}
    review_s = cfg.get('review_s')
    rebalance = not st['portfolio'] or (review_s is None and now - st['chosen_at'] >= REBALANCE_S)
    if review_s and st['portfolio'] and now - st.get('reviewed_at', st['chosen_at']) >= review_s:
        review(st, http, now, log)
        st['reviewed_at'] = now
    if rebalance:
        new = choose(http, now, log, cfg['max_range'])
        if new:
            old = {c['cond']: c for c in st['portfolio']}
            dropped = [c for k, c in old.items() if k not in {n['cond'] for n in new}]
            books = bt.fetch_books([c['yes'] for c in dropped], http) if dropped else {}
            for c in dropped:
                pos = st['positions'].get(c['cond'])
                if pos:
                    close_out(pos, books.get(c['yes']))
                    pos['share'] = None
            st['rebalances'].append({'ts': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                                     'kept': len(new) - len([n for n in new if n['cond'] not in old]),
                                     'added': len([n for n in new if n['cond'] not in old]), 'dropped': len(dropped)})
            st['portfolio'], st['chosen_at'] = new, now
    dt = max(0.0, now - st['last_ts'])
    if not rebalance:
        refresh_rates(st['portfolio'], http, log)
    books = bt.fetch_books([c[k] for c in st['portfolio'] for k in ('yes', 'no')], http)
    tick = {'ts': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'dt_h': round(dt / 3600, 3), 'markets': []}
    reward_tick, shares = 0.0, []
    active = {c['cond'] for c in st['portfolio']}
    for c in st['portfolio']:
        pos = st['positions'].setdefault(c['cond'], {'inv': 0.0, 'cash': 0.0, 'mid': None, 'share': None,
                                                     'reward': 0.0, 'fills': 0, 'q': c['question'][:80]})
        yb, nb = books.get(c['yes']), books.get(c['no'])
        if not yb or not yb.get('bids') or not yb.get('asks'):
            pos['share'] = None
            tick['markets'].append({'q': c['question'][:50], 'status': 'no book'})
            continue
        mid = (max(float(x['price']) for x in yb['bids']) + min(float(x['price']) for x in yb['asks'])) / 2
        share = bt.expected_reward(1.0, c['v'], c['min_size'], c['d'] * 100, bt.book_q(yb, nb or {}, mid, c['v'], c['min_size']))
        prev_mid, prev_share = pos['mid'], pos['share']
        earned = 0.0
        if (dt > 0 and prev_mid is not None and prev_share is not None and 0.10 <= mid <= 0.90
                and 0.10 <= prev_mid <= 0.90 and abs(mid - prev_mid) * 100 <= c['v'] - c['d'] * 100):
            earned = c['rate'] * min(share, prev_share) * dt / 86400                      # R1-R3
        if prev_mid is not None and dt > 0:
            trades = bt.fetch_trades(c['cond'], c['yes'], int(st['last_ts']), http)
            bid, ask, cap = prev_mid - c['d'], prev_mid + c['d'], INV_CAP_MULT * c['min_size']
            bq = c['min_size'] if pos['inv'] + c['min_size'] <= cap else 0.0
            aq = c['min_size'] if -pos['inv'] + c['min_size'] <= cap else 0.0
            for t, p, z in trades:
                if t < st['last_ts'] or t >= now:
                    continue
                if bq > 0 and p <= bid + 1e-9:                                             # R4: touches fill
                    q = min(bq, z); bq -= q; pos['inv'] += q; pos['cash'] -= q * bid; pos['fills'] += 1   # noqa: E702
                    st['fills'].append(fill_row(t, c, 'BUY', bid, q, version))
                elif aq > 0 and p >= ask - 1e-9:
                    q = min(aq, z); aq -= q; pos['inv'] -= q; pos['cash'] += q * ask; pos['fills'] += 1   # noqa: E702
                    st['fills'].append(fill_row(t, c, 'SELL', ask, q, version))
        pos.update(mid=mid, share=share)
        pos['reward'] += earned
        reward_tick += earned
        shares.append(share)
        tick['markets'].append({'q': c['question'][:50], 'mid': round(mid, 3), 'share': round(share, 4),
                                'earned': round(earned, 4), 'inv': pos['inv']})
    # R5 and closed markets: every position counts at its last known midpoint
    mm_value = sum(p['cash'] + p['inv'] * (p['mid'] or 0.0) for p in st['positions'].values())
    st['reward_total'] += reward_tick
    st['last_ts'] = now
    tick.update(reward_tick=round(reward_tick, 4), reward_total=round(st['reward_total'], 2),
                mm_value=round(mm_value, 2), net=round(st['reward_total'] + mm_value, 2),
                avg_share=round(sum(shares) / max(len(shares), 1), 4), n_markets=len(active),
                fills_total=sum(p.get('fills', 0) for p in st['positions'].values()), rebalanced=rebalance)
    add_history(st, {k: tick[k] for k in ('ts', 'dt_h', 'reward_tick', 'reward_total', 'mm_value', 'net',
                                          'avg_share', 'n_markets', 'fills_total', 'rebalanced')}, now)
    st['fills'] = st['fills'][-3000:]
    st['last_tick'] = tick
    save(path, st)
    return tick


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='rewardbot.live_v2')
    ap.add_argument('--state', default='state/rewards')
    ap.add_argument('--version', default='v2', choices=sorted(VERSIONS))
    a = ap.parse_args(argv)
    t = step(a.state, version=a.version)
    print(json.dumps({k: t[k] for k in ('ts', 'dt_h', 'reward_tick', 'reward_total', 'mm_value', 'net',
                                        'avg_share', 'n_markets', 'rebalanced')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
