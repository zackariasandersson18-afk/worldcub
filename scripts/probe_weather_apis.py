"""Compact view of Polymarket daily-temperature events (dev aid, runs in Actions)."""
import json

import requests

S = requests.Session()
G = 'https://gamma-api.polymarket.com'


def get(url, **params):
    r = S.get(url, params=params, timeout=30)
    print('GET', r.url, r.status_code)
    return r.json()


def summarize(events, n=4):
    for e in events[:n]:
        print('\nEVENT', json.dumps({k: e.get(k) for k in (
            'id', 'slug', 'title', 'endDate', 'closed', 'negRisk', 'resolutionSource', 'volume')}))
        print('  tags', [t.get('slug') for t in e.get('tags', [])])
        print('  desc', (e.get('description') or '')[:700].replace('\n', ' '))
        for m in e.get('markets', [])[:12]:
            print('   M', json.dumps({k: m.get(k) for k in (
                'question', 'groupItemTitle', 'groupItemThreshold', 'outcomePrices', 'bestBid', 'bestAsk',
                'lastTradePrice', 'closed', 'endDate', 'feeSchedule', 'orderMinSize')})[:600])


s = get(f'{G}/public-search', q='highest temperature in', limit_per_type=10)
evs = s.get('events', []) if isinstance(s, dict) else []
print('search events:', [e.get('title') for e in evs])
summarize(evs, 2)
for tag in ('daily-temperature', 'temperature', 'weather'):
    data = get(f'{G}/events', tag_slug=tag, closed='false', limit=50)
    titles = [e.get('title') for e in data] if isinstance(data, list) else data
    print(f'tag {tag} open:', len(titles) if isinstance(titles, list) else titles, (titles or [])[:15] if isinstance(titles, list) else '')
temp = [e for e in (get(f'{G}/events', tag_slug='weather', closed='false', limit=200) or [])
        if 'temperature in' in (e.get('title') or '').lower()]
print('\nOPEN daily temperature events:', len(temp))
print(sorted({e['title'].split(' on ')[0] for e in temp}))
summarize(temp, 2)
closed = [e for e in (get(f'{G}/events', tag_slug='weather', closed='true', limit=200,
                          order='endDate', ascending='false') or [])
          if 'temperature in' in (e.get('title') or '').lower()]
print('\nCLOSED daily temperature events (latest 200 weather):', len(closed))
summarize(closed, 1)
if closed:
    m = closed[0]['markets'][0]
    tok = json.loads(m['clobTokenIds'])[0]
    h = get('https://clob.polymarket.com/prices-history', market=tok, interval='max', fidelity=60)
    hist = h.get('history', [])
    print('closed token history points', len(hist), hist[:3], hist[-3:])
