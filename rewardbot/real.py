"""Real money with rewards v4, phase 1: DRY RUN. Places nothing, needs no key.

Every 10 minutes: the exact orders v4's strategy would rest on Polymarket with REAL_CAPITAL USD
(default 50, the owner's choice), from the markets v4 holds right now, best current reward per dollar first:
  per market  BUY YES at mid - d and BUY NO at (1 - mid) - d (a NO bid is a YES ask; two bids need
              no inventory), size = the market's rewards_min_size, prices rounded AWAY from the
              midpoint to the market's tick, skipped if the midpoint is outside [0.10, 0.90], if a
              rounded quote is not strictly inside the max spread v, or if it would cross the book
  capital     sum of size x price over both bids, greedily up to REAL_CAPITAL
  estimate    pot x our share from the live books with these exact prices (same formula as v4)
The plan, and how many orders changed since the last plan (re-quoting churn), go to real_plan.json.
Phase 2 (real orders) is a separate step that needs the owner's key in GitHub Secrets.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from rewardbot import backtest as bt

FILE = 'real_plan.json'
EVERY_S = 600
try:                                                   # GitHub passes an unset variable as '' - never crash the loop on it
    CAPITAL = float((os.environ.get('REAL_CAPITAL') or '').strip() or 50)
except ValueError:
    CAPITAL = 50.0
BAND = (0.10, 0.90)


def _round(p: float, tick: float, down: bool) -> float:
    n = p / tick
    return round((math.floor(n + 1e-9) if down else math.ceil(n - 1e-9)) * tick, 6)


def quote(c: dict, yb: dict, nb: dict, tick: float) -> dict | None:
    """The two bids for one market, or None with no valid quote."""
    if not yb or not yb.get('bids') or not yb.get('asks'):
        return None
    best_bid = max(float(x['price']) for x in yb['bids'])
    best_ask = min(float(x['price']) for x in yb['asks'])
    mid = (best_bid + best_ask) / 2
    if not BAND[0] <= mid <= BAND[1]:
        return None
    v = c['v'] / 100
    yes = _round(mid - c['d'], tick, down=True)                 # further from mid, never closer
    no = _round((1 - mid) - c['d'], tick, down=True)            # NO bid at q  ==  YES ask at 1 - q
    dy, dn = mid - yes, (1 - no) - mid
    if yes <= 0 or no <= 0 or dy >= v or dn >= v:
        return None
    no_asks = [float(x['price']) for x in (nb or {}).get('asks') or []]
    if yes >= best_ask or (no_asks and no >= min(no_asks)):
        return None                                             # would trade at once instead of resting
    size = float(c['min_size'])
    rest = bt.book_q(yb, nb or {}, mid, c['v'], c['min_size'])
    ours = bt.qmin(bt.score(dy * 100, c['v'], size), bt.score(dn * 100, c['v'], size))
    share = ours / (ours + bt.qmin(*rest)) if ours > 0 else 0.0
    return {'mid': round(mid, 4), 'yes_bid': yes, 'no_bid': no, 'size': size, 'share': round(share, 4),
            'cost': round(size * (yes + no), 2), 'est_day': round(c['rate'] * share, 2)}


KEEP_BONUS = 1.3      # a market we already quote must be beaten by 30 % before we move (no flip-flopping each minute)


def without_own(book: dict, own: list) -> dict:
    """The book minus our own resting orders (price, size), so we never count ourselves as competition."""
    if not book or not own:
        return book
    left = {}
    for p, z in own:
        left[round(float(p), 4)] = left.get(round(float(p), 4), 0.0) + float(z)
    out = {**book, 'bids': []}
    for lv in book.get('bids') or []:
        p = round(float(lv['price']), 4)
        z = float(lv['size']) - left.pop(p, 0.0)
        if z > 1e-9:
            out['bids'].append({**lv, 'size': str(z)})
    return out


def _up(p: float, tick: float) -> float:
    return _round(p, tick, down=False)


def sell_price(token_is_yes: bool, mid: float, d: float, avg: float, tick: float) -> float:
    """Where to offer shares we hold: on the ask side at the quote distance (that order earns rewards
    like the bid it replaces), never below what we paid - a fallen position waits at cost instead."""
    target = (mid + d) if token_is_yes else ((1 - mid) + d)
    return min(_up(max(target, avg), tick), round(1 - tick, 6))


def compute(state: str, http, capital: float, ticks: dict, held: dict | None = None, own: dict | None = None,
            avg: dict | None = None) -> dict:
    """Markets and orders for `capital` USD, chosen from v4's whole portfolio by the LIVE reward per dollar:
    every market is priced against the order book right now (our own orders taken out), and the best
    ones are taken until the capital is used. held: token -> shares we own (no more bids for those, v4's
    inventory cap of 1 x min size). own: token -> [(price, size)] of our resting orders; a market we already
    quote keeps a 30 % head start so the bot does not hop between markets every minute.
    avg: token -> average price paid. Shares we hold are offered for sale (SELL orders, no cash needed) on
    the ask side instead of buying the opposite token - see sell_price - in every market we hold them in."""
    d = Path(state)
    v4 = json.loads((d / 'rewards_v4_state.json').read_text())
    port = v4.get('portfolio') or []
    books = bt.fetch_books([c[k] for c in port for k in ('yes', 'no')], http)
    held, own, avg = held or {}, own or {}, avg or {}
    cands = []
    for c in port:
        if c['cond'] not in ticks:
            m = bt._get(http, f"{bt.CLOB}/markets/{c['cond']}") or {}
            ticks[c['cond']] = float(m.get('minimum_tick_size') or 0.01)
        yb = without_own(books.get(c['yes']), own.get(c['yes']))
        nb = without_own(books.get(c['no']), own.get(c['no']))
        q = quote(c, yb, nb, ticks[c['cond']])
        if not q or q['est_day'] <= 0 or q['cost'] <= 0:
            continue
        mine = c['yes'] in own or c['no'] in own
        cands.append({'q': c['question'][:80], 'cond': c['cond'], 'yes': c['yes'], 'no': c['no'], 'rate': c['rate'],
                      'v_c': c['v'], 'tick': ticks[c['cond']], 'quoted': mine, 'per_dollar': round(q['est_day'] / q['cost'], 4),
                      '_rank': q['est_day'] / q['cost'] * (KEEP_BONUS if mine else 1.0), **q})
    cands.sort(key=lambda r: -r['_rank'])
    orders, markets, used = [], [], 0.0
    for r in cands:
        if used + r['cost'] > capital:
            continue
        used += r['cost']
        markets.append(r)
        for tok, other, outcome, price in ((r['yes'], r['no'], 'YES', r['yes_bid']), (r['no'], r['yes'], 'NO', r['no_bid'])):
            if held.get(tok, 0) + 1e-9 >= r['size']:
                continue                                    # inventory cap: do not buy more of what we hold
            if held.get(other, 0) >= 1:
                continue                                    # that side is covered by selling the shares we hold
            orders.append({'cond': r['cond'], 'token': tok, 'outcome': outcome, 'side': 'BUY', 'price': price, 'size': r['size']})
    # offer every position we hold, in any market of v4's portfolio
    for c in port:
        for tok, is_yes, outcome in ((c['yes'], True, 'YES'), (c['no'], False, 'NO')):
            size = round(held.get(tok, 0), 2)
            if size < 1:
                continue
            yb = without_own(books.get(c['yes']), own.get(c['yes']))
            if not yb or not yb.get('bids') or not yb.get('asks'):
                continue
            mid = (max(float(x['price']) for x in yb['bids']) + min(float(x['price']) for x in yb['asks'])) / 2
            price = sell_price(is_yes, mid, c['d'], float(avg.get(tok) or 0), ticks.get(c['cond'], 0.01))
            orders.append({'cond': c['cond'], 'token': tok, 'outcome': outcome, 'side': 'SELL', 'price': price, 'size': size,
                           'q': c['question'][:80], 'avg': avg.get(tok), 'mid': round(mid, 4)})
    strip = lambda r: {k: v for k, v in r.items() if k != '_rank'}   # noqa: E731
    chosen = {r['cond'] for r in markets}
    return {'used': round(used, 2), 'est_reward_day': round(sum(m['est_day'] for m in markets), 2),
            'markets': [strip(r) for r in markets], 'orders': orders,
            'alternatives': [strip(r) for r in cands if r['cond'] not in chosen][:8], 'n_priced': len(cands)}


def maybe_plan(state: str, http, now: float | None = None, log=print) -> dict | None:
    now = now or time.time()
    d = Path(state)
    path = d / FILE
    old = json.loads(path.read_text()) if path.exists() else {}
    if now - old.get('ts_unix', 0) < EVERY_S or not (d / 'rewards_v4_state.json').exists():
        return None
    ticks = dict(old.get('ticks') or {})
    c = compute(state, http, CAPITAL, ticks)
    key = lambda o: (o['token'], o['price'], o['size'])   # noqa: E731
    before = {key(o) for o in old.get('orders') or []}
    after = {key(o) for o in c['orders']}
    plan = {'mode': 'dry-run', 'ts': time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(now)), 'ts_unix': now, 'capital': CAPITAL,
            **c, 'changes': {'new': len(after - before), 'cancelled': len(before - after)} if before else None, 'ticks': ticks}
    hist = (old.get('history') or []) + [{'ts': plan['ts'], 'est_reward_day': plan['est_reward_day'], 'used': plan['used'],
                                          'n': len(c['markets']), **(plan['changes'] or {})}]
    plan['history'] = hist[-1000:]
    path.write_text(json.dumps(plan, separators=(',', ':')))
    log(f"real dry-run {plan['ts']}: {len(c['markets'])} markets, {c['used']:.0f}/{CAPITAL:.0f} USD, est {plan['est_reward_day']} USD/day")
    return plan
