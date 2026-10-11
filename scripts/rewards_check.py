"""Read-only: how new are the rewards programmes? Start dates across the whole listing, and for the
markets rewards v4 holds, when the market itself was created (Gamma) and how long it has traded."""
import collections
import json
import time

import requests

from rewardbot import backtest as bt

http = requests.Session()
rm = bt._reward_markets(http, print)
starts = collections.Counter((m.get('rewards_config') or [{}])[0].get('start_date') for m in rm)
ends = collections.Counter((m.get('rewards_config') or [{}])[0].get('end_date') for m in rm)
print('start_date top:', starts.most_common(12))
print('end_date top:', ends.most_common(5))
rates = collections.Counter(round(sum(c.get('rate_per_day', 0) for c in m.get('rewards_config') or [])) for m in rm)
print('rate_per_day top:', rates.most_common(12))
st = http.get('https://raw.githubusercontent.com/zackariasandersson18-afk/worldcub/rewards-state/rewards/rewards_v4_state.json', timeout=30).json()
conds = [c['cond'] for c in st['portfolio']]
info = bt.gamma_info(conds, http)
now = time.time()
ages, first_trade = [], []
for c in st['portfolio']:
    g = info.get(c['cond']) or {}
    created = g.get('createdAt') or g.get('startDate')
    tr = []
    for off in (0, 500, 1000, 1500):
        page = bt._get(http, f"{bt.DATA_API}/trades", {'market': c['cond'], 'limit': 500, 'offset': off}) or []
        tr += page
        if len(page) < 500:
            break
    oldest = min((int(t['timestamp']) for t in tr), default=None)
    row = {'q': c['question'][:60], 'rate': c['rate'], 'created': created, 'clobRewards_start': [r.get('startDate') for r in g.get('clobRewards') or []],
           'trades_seen': len(tr), 'oldest_trade_days': round((now - oldest) / 86400, 1) if oldest else None}
    print(json.dumps(row))
