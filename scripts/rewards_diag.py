"""One-off diagnostic: are version 1's frozen reward rates still current, and why did
version 2's selection find so little? Read-only; prints only."""
import json
import subprocess
import time

import requests

from rewardbot import backtest as bt, live_v2

http = requests.Session()
st = json.loads(subprocess.run(['git', 'show', 'origin/tradebot-state:rewards/rewards_state.json'],
                               capture_output=True, text=True, check=True).stdout)
rm = bt.reward_markets(http)
now_rate = {m['condition_id']: float(sum(c.get('rate_per_day', 0) for c in m.get('rewards_config') or [])) for m in rm}
print('\n== version 1 markets: frozen rate vs rate now (missing = no longer in the programme listing)')
tot_then = tot_now = 0
for c in st['portfolio']:
    r = now_rate.get(c['cond'])
    tot_then += c['rate']; tot_now += r or 0
    print(f"{c['question'][:55]:55s} then {c['rate']:7.2f}  now {('%7.2f' % r) if r is not None else 'MISSING'}")
print(f'total pool/day then {tot_then:.2f}  now {tot_now:.2f}')

print('\n== raw listing: pages')
d = bt._get(http, bt.REWARDS_API, None)
print('first page', len(d.get('data') or []), 'next_cursor', d.get('next_cursor'), 'keys', list(d.keys()))

print('\n== version 2 candidates, ranked')
now = time.time()
rows = []
for c in bt.candidates(http):
    dd = round(c['v'] / 200, 4)
    rew = bt.expected_reward(c['rate'], c['v'], c['min_size'], dd * 100, c['rest_q'])
    coll = c['min_size'] * (c['mid'] - dd) + c['min_size'] * (1 - c['mid'] - dd)
    rows.append((rew / coll if coll > 0 else 0, rew, coll, c))
rows.sort(key=lambda r: -r[0])
for pdol, rew, coll, c in rows[:30]:
    px = [p for _, p, _ in bt.fetch_trades(c['cond'], c['yes'], int(now - live_v2.TREND_LOOKBACK_S), http)]
    rng = (max(px) - min(px)) if px else 0
    print(f"{c['question'][:50]:50s} rate {c['rate']:6.2f} est/day {rew:6.2f} coll {coll:6.1f} mid {c['mid']:.3f} "
          f"range72h {rng:.3f} trades {len(px)} v {c['v']} min {c['min_size']}")
