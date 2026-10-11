"""How often do Polymarket's reward pots change? Read only; no effect on any bot.

Runs at most once an hour, and only when the shared rewards listing cache is fresh (the v4 hourly review
fills it), so it never downloads the 190-page listing itself.
  hourly  every pot of >= 10 USD/day is compared with the last run: rises, cuts, appearances and removals
          are logged as events (with whether rewards v4 holds that market)
  daily   all market ids are compared with yesterday's: how many markets entered and left the programme
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from rewardbot import backtest as bt

FILE = 'rewards_census.json'
IDS_FILE = 'rewards_census_ids.json'
MIN_RATE = 10
EVERY_S = 3600
KEY = 14                                              # condition id prefix: unique enough, keeps the files small


def _rate(m: dict) -> float:
    return round(sum(c.get('rate_per_day', 0) or 0 for c in m.get('rewards_config') or []), 2)


def maybe_run(state: str, now: float | None = None, log=print, listing: list | None = None) -> dict | None:
    now = now or time.time()
    d = Path(state)
    path = d / FILE
    cen = json.loads(path.read_text()) if path.exists() else {'snapshot': {}, 'events': [], 'days': [], 'hours': []}
    if now - cen.get('last_ts', 0) < EVERY_S:
        return None
    if listing is None:
        cache = bt.LISTING_CACHE
        if not cache or not os.path.exists(cache) or now - os.path.getmtime(cache) > bt.LISTING_MAX_AGE_S:
            return None
        listing = json.loads(Path(cache).read_text())
    if not listing:
        return None
    v4 = set()
    v4p = d / 'rewards_v4_state.json'
    if v4p.exists():
        v4 = {c['cond'][:KEY] for c in json.loads(v4p.read_text()).get('portfolio') or []}
    iso = time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(now))
    snap = {m['condition_id'][:KEY]: _rate(m) for m in listing if m.get('condition_id') and _rate(m) >= MIN_RATE}
    rates = {m['condition_id'][:KEY]: _rate(m) for m in listing if m.get('condition_id')}
    names = {m['condition_id'][:KEY]: (m.get('question') or '')[:60] for m in listing if m.get('condition_id')}
    old = cen['snapshot']
    ev = []
    if old:
        for k in set(old) | set(snap):
            a, b = old.get(k), snap.get(k)
            if a != b:
                if b is None and k in rates:           # still paying, just below MIN_RATE now
                    b = rates[k]
                kind = 'över 10' if a is None else 'borta' if b is None else 'upp' if b > a else 'ner'
                ev.append({'ts': iso, 'id': k, 'q': names.get(k, ''), 'from': a, 'to': b, 'kind': kind, 'v4': k in v4})
    cen['events'] = (cen['events'] + ev)[-20000:]
    hour = {'ts': iso, 'n_all': len(listing), 'n_10plus': len(snap), 'sum_10plus': round(sum(snap.values())),
            'changes': len(ev), 'up': sum(e['kind'] == 'upp' for e in ev), 'down': sum(e['kind'] == 'ner' for e in ev),
            'v4_changes': sum(e['v4'] for e in ev), 'baseline': not old}
    cen['hours'] = (cen['hours'] + [hour])[-24 * 60:]
    day = iso[:10]
    if not cen['days'] or cen['days'][-1]['date'] != day:   # first run of a UTC day: who entered and left
        ids_path = d / IDS_FILE
        ids = {m['condition_id'][:KEY] for m in listing if m.get('condition_id')}
        prev = set(json.loads(ids_path.read_text())) if ids_path.exists() else None
        cen['days'].append({'date': day, 'n_all': len(ids), 'new': len(ids - prev) if prev is not None else None,
                            'gone': len(prev - ids) if prev is not None else None,
                            'new_10plus': sorted(snap.get(k, 0) for k in ids - prev)[-10:] if prev is not None else None})
        ids_path.write_text(json.dumps(sorted(ids), separators=(',', ':')))
    cen['snapshot'], cen['last_ts'] = snap, now
    path.write_text(json.dumps(cen, separators=(',', ':')))
    log(f"census {iso}: {len(ev)} pot changes among {len(snap)} pots >= {MIN_RATE} USD/day ({hour['v4_changes']} in v4's markets)")
    return hour
