"""One-off: print sample responses of the free APIs the memecoin bot will use."""
import json

import requests

h = requests.Session()
h.headers['accept'] = 'application/json'


def show(name, url, n=3000):
    try:
        r = h.get(url, timeout=30)
        print(f'\n===== {name} {r.status_code} {url}')
        print(r.text[:n])
        return r.json()
    except Exception as exc:
        print(name, 'ERR', exc)


d = show('gecko new_pools', 'https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=1&include=base_token,quote_token,dex', 6000)
pool = (d or {}).get('data', [{}])[0]
addr = pool.get('attributes', {}).get('address')
base = ((pool.get('relationships') or {}).get('base_token') or {}).get('data', {}).get('id', '').replace('solana_', '')
print('\nPOOL', addr, 'BASE', base)
show('gecko token info', f'https://api.geckoterminal.com/api/v2/networks/solana/tokens/{base}/info', 3000)
show('rugcheck summary', f'https://api.rugcheck.xyz/v1/tokens/{base}/report/summary', 4000)
show('rugcheck report', f'https://api.rugcheck.xyz/v1/tokens/{base}/report', 5000)
show('dexscreener pair', f'https://api.dexscreener.com/latest/dex/pairs/solana/{addr}', 3000)
show('dexscreener tokens', f'https://api.dexscreener.com/tokens/v1/solana/{base}', 3000)
show('frankfurter', 'https://api.frankfurter.app/latest?from=USD&to=SEK', 300)
