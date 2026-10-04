"""Dev aid: check the browser data sources for the dashboard (runs in Actions)."""
import json
import requests

def show(url, n=700):
    try:
        r = requests.get(url, timeout=20, headers={'Origin': 'https://example.com'})
        print('\n', r.status_code, url, '| CORS:', r.headers.get('access-control-allow-origin'))
        print(r.text[:n])
    except Exception as e:
        print('ERR', url, e)

syms = json.dumps(["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT"], separators=(',', ':'))
for w in ('5m', '15m', '1h', '4h', '1d', '7d'):
    show(f'https://data-api.binance.vision/api/v3/ticker?symbols={syms}&windowSize={w}&type=MINI', 400 if w != '5m' else 900)
show('https://api.frankfurter.app/latest?from=USD&to=SEK')
show('https://api.frankfurter.dev/v1/latest?base=USD&symbols=SEK')
show('https://open.er-api.com/v6/latest/USD', 300)
show('https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=2')
