"""Dev aid: data formats for testing suislanchez's two strategies (runs in Actions)."""
import json, time
import requests
S = requests.Session()
G = 'https://gamma-api.polymarket.com'
now = int(time.time()) // 300 * 300
for back in (3600, 3600 * 24, 3600 * 24 * 30):
    ts = now - back
    r = S.get(f'{G}/events', params={'slug': f'btc-updown-5m-{ts}'}, timeout=20)
    d = r.json()
    print('\n== slug ts', ts, 'back', back, 'status', r.status_code, 'n', len(d))
    if d:
        e = d[0]; m = e['markets'][0]
        print(json.dumps({k: e.get(k) for k in ('slug', 'title', 'startDate', 'endDate', 'closed', 'startTime', 'eventStartTime')}))
        print(json.dumps({k: m.get(k) for k in ('question', 'outcomes', 'outcomePrices', 'startDate', 'endDate', 'eventStartTime',
                                                 'closed', 'feeSchedule', 'orderMinSize', 'volume', 'clobTokenIds', 'umaResolutionStatus')})[:900])
        print('desc:', (e.get('description') or '')[:400])
        tok = json.loads(m['clobTokenIds'])[0]
        h = S.get('https://clob.polymarket.com/prices-history', params={'market': tok, 'interval': 'max', 'fidelity': 1}, timeout=20).json()
        hist = h.get('history', [])
        print('history points', len(hist), hist[:3], hist[-2:])
# how many 5m events does the series have (listing)
r = S.get(f'{G}/events', params={'series_slug': 'btc-up-or-down-5m', 'closed': 'true', 'limit': 3, 'order': 'endDate', 'ascending': 'false'}, timeout=20)
print('\nseries listing', r.status_code, [e.get('slug') for e in r.json()] if r.ok else r.text[:200])
# Open-Meteo GFS ensemble format
r = S.get('https://ensemble-api.open-meteo.com/v1/ensemble', params={'latitude': 40.78, 'longitude': -73.87, 'hourly': 'temperature_2m',
          'models': 'gfs_seamless', 'timezone': 'auto', 'forecast_days': 2, 'temperature_unit': 'fahrenheit'}, timeout=30)
d = r.json(); keys = list(d.get('hourly', {}).keys())
print('\nensemble', r.status_code, 'keys', len(keys), keys[:4], keys[-2:], 'offset', d.get('utc_offset_seconds'))
r = S.get('https://ensemble-api.open-meteo.com/v1/ensemble', params={'latitude': 40.78, 'longitude': -73.87, 'hourly': 'temperature_2m',
          'models': 'gfs_seamless', 'start_date': '2026-09-01', 'end_date': '2026-09-02'}, timeout=30)
print('ensemble history', r.status_code, r.text[:200])
