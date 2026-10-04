"""Dev aid: can a browser call these APIs (CORS)? Runs in Actions."""
import requests

H = {'Origin': 'https://example.com'}
urls = [
    'https://gamma-api.polymarket.com/events?tag_slug=highest-temperature&closed=false&limit=2',
    'https://clob.polymarket.com/book?token_id=0',
    'https://clob.polymarket.com/prices-history?market=0&interval=1d',
    'https://api.open-meteo.com/v1/forecast?latitude=51.5&longitude=0&hourly=temperature_2m&forecast_days=1',
    'https://raw.githubusercontent.com/zackariasandersson18-afk/worldcub/claude/football-betting-optimizer-45ox0q/weatherbot/stations.json',
]
for u in urls:
    try:
        r = requests.get(u, headers=H, timeout=20)
        print(r.status_code, 'CORS:', r.headers.get('access-control-allow-origin'), u)
    except Exception as e:
        print('ERR', u, e)
# preflight-free POST for clob /books (several books in one call)?
r = requests.post('https://clob.polymarket.com/books', json=[{'token_id': '0'}], headers=H, timeout=20)
print(r.status_code, 'CORS:', r.headers.get('access-control-allow-origin'), 'POST /books', r.text[:200])
r = requests.options('https://clob.polymarket.com/books', headers={**H, 'Access-Control-Request-Method': 'POST',
                     'Access-Control-Request-Headers': 'content-type'}, timeout=20)
print('OPTIONS /books', r.status_code, dict((k, v) for k, v in r.headers.items() if k.lower().startswith('access-control')))
