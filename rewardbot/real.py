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
CAPITAL = float(os.environ.get('REAL_CAPITAL', '50'))
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


def compute(state: str, http, capital: float, ticks: dict, held: dict | None = None) -> dict:
    """Markets and orders for `capital` USD from v4's current portfolio. held: token -> shares we own;
    a bid is left out when we already hold min_size of that token (v4's inventory cap of 1 x min size)."""
    d = Path(state)
    v4 = json.loads((d / 'rewards_v4_state.json').read_text())
    pos = v4.get('positions') or {}
    ranked = sorted(v4.get('portfolio') or [], key=lambda c: -(c['rate'] * ((pos.get(c['cond']) or {}).get('share') or 0))
                    / max(c['collateral'], 1e-9))[:30]
    books = bt.fetch_books([c[k] for c in ranked for k in ('yes', 'no')], http)
    held = held or {}
    orders, markets, used = [], [], 0.0
    for c in ranked:
        if c['cond'] not in ticks:
            m = bt._get(http, f"{bt.CLOB}/markets/{c['cond']}") or {}
            ticks[c['cond']] = float(m.get('minimum_tick_size') or 0.01)
        q = quote(c, books.get(c['yes']), books.get(c['no']), ticks[c['cond']])
        if not q or q['est_day'] <= 0 or used + q['cost'] > capital:
            continue
        used += q['cost']
        markets.append({'q': c['question'][:80], 'cond': c['cond'], 'rate': c['rate'], 'v_c': c['v'], 'tick': ticks[c['cond']], **q})
        for tok, outcome, price in ((c['yes'], 'YES', q['yes_bid']), (c['no'], 'NO', q['no_bid'])):
            if held.get(tok, 0) + 1e-9 >= q['size']:
                continue                                    # inventory cap: do not buy more of what we hold
            orders.append({'cond': c['cond'], 'token': tok, 'outcome': outcome, 'side': 'BUY', 'price': price, 'size': q['size']})
    return {'used': round(used, 2), 'est_reward_day': round(sum(m['est_day'] for m in markets), 2), 'markets': markets, 'orders': orders}


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
