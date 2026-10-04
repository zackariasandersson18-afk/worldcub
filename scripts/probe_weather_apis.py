"""Print real sample responses from the APIs the weather bot will use.
Runs in GitHub Actions (the dev sandbox cannot reach them)."""
import json
import sys

import requests

S = requests.Session()
S.headers['User-Agent'] = 'tradebot-weather-probe'


def show(label, url, params=None, limit=3000):
    print(f'\n===== {label}\nGET {url} {params or ""}')
    try:
        r = S.get(url, params=params, timeout=30)
        print('status', r.status_code, r.headers.get('content-type'))
        txt = r.text
        try:
            data = r.json()
            txt = json.dumps(data, indent=1)[:limit]
        except Exception:
            txt = txt[:limit]
        print(txt)
        return r.json() if 'json' in (r.headers.get('content-type') or '') else None
    except Exception as exc:
        print('ERROR', exc)
        return None


G = 'https://gamma-api.polymarket.com'
search = show('gamma public-search', f'{G}/public-search', {'q': 'highest temperature', 'limit_per_type': 3}, 6000)
show('gamma tags', f'{G}/tags', {'limit': 100}, 4000)
ev = show('gamma events open', f'{G}/events', {'closed': 'false', 'limit': 3, 'tag_slug': 'weather'}, 6000)
evc = show('gamma events closed', f'{G}/events', {'closed': 'true', 'limit': 2, 'tag_slug': 'weather',
                                                   'order': 'endDate', 'ascending': 'false'}, 6000)

# pick a token id from whatever came back
token = None
for blob in (ev, evc, search):
    events = blob if isinstance(blob, list) else (blob or {}).get('events', [])
    for e in events or []:
        for m in e.get('markets', []):
            ids = m.get('clobTokenIds')
            if ids:
                token = json.loads(ids)[0] if isinstance(ids, str) else ids[0]
                print('\nusing token', token, 'from', m.get('question'))
                break
        if token:
            break
    if token:
        break
if token:
    C = 'https://clob.polymarket.com'
    show('clob book', f'{C}/book', {'token_id': token})
    show('clob prices-history', f'{C}/prices-history', {'market': token, 'interval': 'max', 'fidelity': 60})

O = 'https://api.open-meteo.com/v1/forecast'
show('open-meteo multi-model daily max (London City EGLC)', O, {
    'latitude': 51.5048, 'longitude': 0.0522, 'daily': 'temperature_2m_max',
    'models': 'ecmwf_ifs025,ecmwf_aifs025_single,gfs_seamless,icon_seamless,gem_seamless,jma_seamless',
    'timezone': 'Europe/London', 'forecast_days': 3})
show('open-meteo previous runs hourly (lead 1 day)', 'https://previous-runs-api.open-meteo.com/v1/forecast', {
    'latitude': 51.5048, 'longitude': 0.0522, 'hourly': 'temperature_2m_previous_day1',
    'models': 'ecmwf_ifs025,gfs_seamless', 'timezone': 'Europe/London',
    'start_date': '2026-09-01', 'end_date': '2026-09-02'})
show('IEM ASOS archive (EGLC)', 'https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py', {
    'station': 'EGLC', 'data': 'tmpc', 'year1': 2026, 'month1': 9, 'day1': 1,
    'year2': 2026, 'month2': 9, 'day2': 2, 'tz': 'Etc/UTC', 'format': 'onlycomma', 'missing': 'empty'}, 1500)
sys.exit(0)
