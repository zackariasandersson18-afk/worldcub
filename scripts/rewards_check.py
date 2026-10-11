"""Sanity check of rewards v4 (no orders, read only): for the markets v4 thinks pay best, compare its
assumptions with Polymarket's own data - the full rewards config, the CLOB market's reward settings,
the live books near the midpoint, and recent trading."""
import json
import sys
import time

import requests

from rewardbot import backtest as bt

http = requests.Session()
st = http.get('https://raw.githubusercontent.com/zackariasandersson18-afk/worldcub/rewards-state/rewards/rewards_v4_state.json', timeout=30).json()
pf = sorted(st['portfolio'], key=lambda c: -c['est_reward_day'] / max(c['collateral'], 1e-9))[:12]
print(f"v4: {len(st['portfolio'])} markets, sum est_reward_day {sum(c['est_reward_day'] for c in st['portfolio']):.0f} USD/day, "
      f"collateral {sum(c['collateral'] for c in st['portfolio']):.0f} USD")
listing = {m['condition_id']: m for m in bt._reward_markets(http, print)}
out = []
for c in pf:
    m = listing.get(c['cond'])
    cm = bt._get(http, f"{bt.CLOB}/markets/{c['cond']}")
    g = (bt._get(http, f"{bt.GAMMA}/markets", [('condition_ids', c['cond'])]) or [{}])[0]
    books = bt.fetch_books([c['yes'], c['no']], http)
    yb, nb = books.get(c['yes']) or {}, books.get(c['no']) or {}
    bid = max([float(x['price']) for x in yb.get('bids') or []] or [0])
    ask = min([float(x['price']) for x in yb.get('asks') or []] or [1])
    mid = (bid + ask) / 2
    near = lambda lv, f: [(round(f(float(x['price'])), 3), round(float(x['size']), 1)) for x in lv or []
                          if abs(f(float(x['price'])) - mid) * 100 < c['v'] + 2]
    trades = bt._get(http, f"{bt.DATA_API}/trades", {'market': c['cond'], 'limit': 50}) or []
    row = {'q': c['question'][:80], 'v4_rate': c['rate'], 'v4_share0': round(c['share0'], 3), 'v4_est_day': round(c['est_reward_day'], 2),
           'collateral': round(c['collateral'], 2), 'v_c': c['v'], 'min_size': c['min_size'], 'd': c['d'],
           'listing_rewards_config': (m or {}).get('rewards_config'), 'listing_competitiveness': (m or {}).get('market_competitiveness'),
           'listing_keys': sorted((m or {}).keys()),
           'clob_rewards': (cm or {}).get('rewards'), 'clob_active': (cm or {}).get('active'), 'clob_closed': (cm or {}).get('closed'),
           'clob_accepting': (cm or {}).get('accepting_orders'),
           'gamma': {k: g.get(k) for k in ('volume24hr', 'liquidityNum', 'spread', 'bestBid', 'bestAsk', 'lastTradePrice', 'rewardsMinSize',
                                            'rewardsMaxSpread', 'clobRewards', 'competitive', 'acceptingOrders', 'endDate')},
           'book_mid': round(mid, 4), 'best': [bid, ask],
           'yes_bids_near': near(yb.get('bids'), lambda p: p), 'yes_asks_near': near(yb.get('asks'), lambda p: p),
           'no_bids_near_in_yes': near(nb.get('bids'), lambda p: 1 - p), 'no_asks_near_in_yes': near(nb.get('asks'), lambda p: 1 - p),
           'rest_q_now': [round(x, 1) for x in bt.book_q(yb, nb, mid, c['v'], c['min_size'])],
           'trades_last_50': len(trades), 'last_trade_age_h': round((time.time() - int(trades[0]['timestamp'])) / 3600, 1) if trades else None}
    out.append(row)
    print(json.dumps(row, default=str))
json.dump(out, open('rewards_check.json', 'w'), indent=1, default=str)
