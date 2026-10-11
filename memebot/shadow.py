"""Shadow book: what happened to the tokens the bots looked at, bought or not. Display and research only.

Tracked: every Solana candidate (SOL/USDC quote, 10 min - 6 h old, liquidity >= 10 000 USD) that passes all
cheap market filters, or fails exactly one of liquidity / buy pressure / volume / price change. Recorded
once at first sighting (features, every failed filter, RugCheck verdict), then its price and liquidity
every 30 minutes for 48 h (coarse on purpose: the files are committed every 10 minutes). Finished tokens
move to shadow/YYYY-MM-DD.json by the day they were first seen. Lets later rule ideas be tested on the same tokens without waiting for new
trades; every such test still counts as a trial.
"""
from __future__ import annotations

import time
from pathlib import Path

from memebot import paper

EVERY_S = 1800
HOURS = 48
MAX_LIVE = 120
NEAR = {'liquidity', 'buy pressure', 'volume', 'price change'}
NEW_REPORTS_PER_TICK = 5


def wanted(c: dict) -> list[str] | None:
    """Failed filters if the candidate belongs in the shadow book, else None."""
    if c['quote'] not in paper.QUOTES or not paper.AGE_MIN_S <= c['age_s'] <= paper.AGE_MAX_S or c['liq'] < 10_000 or c['price'] <= 0:
        return None
    f = paper.market_fails(c)
    return f if len(f) == 0 or (len(f) == 1 and f[0] in NEAR) else None


def update(state_dir, market, now: float | None = None, log=print) -> dict:
    d = Path(state_dir)
    path = d / 'shadow.json'
    sh = paper.load(path, {'about': __doc__.split('\n')[0], 'live': [], 'recent': []})
    now = now or time.time()
    known = {x['pool'] for x in sh['live']} | set(sh['recent'])
    reports = 0
    for c in market.cands():
        if c['pool'] in known or len(sh['live']) >= MAX_LIVE:
            continue
        f = wanted(c)
        if f is None:
            continue
        verdict = 'unchecked'
        if c['mint'] in market.reports or reports < NEW_REPORTS_PER_TICK:
            reports += c['mint'] not in market.reports
            verdict = paper.safety(market.report(c['mint'])) or 'ok'
        known.add(c['pool'])
        sh['live'].append({'pool': c['pool'], 'mint': c['mint'], 'name': c['name'], 't0': int(now), 'age_s': int(c['age_s']),
                           'liq': round(c['liq']), 'price': float(f"{c['price']:.6g}"), 'm5': c['m5'], 'vol_h1': round(c['vol_h1']),
                           'chg_h1': c['chg_h1'], 'fails': f, 'safety': verdict,
                           'path': [[0, float(f"{c['price']:.4g}"), round(c['liq'] / 1000)]]})
    due = [x for x in sh['live'] if now - x['t0'] - x['path'][-1][0] * 60 >= EVERY_S]
    q = paper.quotes(market.http, [x['pool'] for x in due]) if due else {}
    for x in due:
        o = q.get(x['pool'])
        px, lq = (o['price'], o['liq']) if o else (0.0, 0.0)  # 0 = no price this time (pair gone or no answer)
        x['path'].append([int((now - x['t0']) // 60), float(f'{px:.4g}'), round(lq / 1000)])
    finished = [x for x in sh['live'] if now - x['t0'] >= HOURS * 3600]
    if finished:
        (d / 'shadow').mkdir(exist_ok=True)
    for x in finished:
        sh['live'].remove(x)
        day = d / 'shadow' / f"{time.strftime('%Y-%m-%d', time.gmtime(x['t0']))}.json"
        arch = paper.load(day, [])
        arch.append(x)
        day.write_text(paper.json.dumps(arch, separators=(',', ':')))
        sh['recent'].append(x['pool'])
    sh['recent'] = sh['recent'][-3000:]                # never track the same pool twice
    sh['updated'] = int(now)
    path.write_text(paper.json.dumps(sh, separators=(',', ':')))
    return {'live': len(sh['live']), 'recent': len(sh['recent'])}
