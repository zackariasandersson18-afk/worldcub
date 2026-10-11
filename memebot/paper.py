"""Virtual memecoin trading on Solana (paper money only). Trial 66.

Rules fixed BEFORE any result was seen:
  pot        5000 kr; 250 kr per buy; at most 10 open positions; one entry per token ever
  universe   Solana pools from GeckoTerminal (trending 1 h, pages 1-2, and new pools, pages
             1-3), quote token SOL or USDC, pool 10 minutes to 6 hours old
  entry      all of:
             liquidity (reserve_in_usd) >= 20 000 USD
             last 5 minutes: buys >= 1.5 x sells and >= 20 distinct buyers
             1 h volume >= 0.5 x liquidity
             1 h price change between 0 % and +100 % (rising, not already pumped)
             RugCheck: no risk of level "danger", LP >= 90 % locked or burned, no mint or
             freeze authority, no transfer fee; top 10 holders (pools/AMMs excluded) <= 30 %
  costs      2 % per side (DEX fee + priority fee + slippage)
  exits      checked every minute at the DexScreener price:
             price >= 2 x entry: sell half (once)
             before that: price <= 0.6 x entry -> sell all (stop -40 %)
             after that: price >= 5 x entry, or <= 0.5 x the peak -> sell the rest
             48 h after entry -> sell all
             pair gone or liquidity < 1000 USD (3 minutes in a row) -> total loss (rug)
  equity     cash + open positions at the current price, after the exit cost
Gaps between minutes are real: a stop fills at the price seen, which can be far below -40 %.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

N_TRIAL = 66
START_KR = 5000.0
STAKE_KR = 250.0
MAX_OPEN = 10
COST = 0.02
MIN_LIQ = 20_000
AGE_MIN_S, AGE_MAX_S = 600, 6 * 3600
QUOTES = {'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'}
GT = 'https://api.geckoterminal.com/api/v2/networks/solana'
RC = 'https://api.rugcheck.xyz/v1/tokens'
DS = 'https://api.dexscreener.com/latest/dex/pairs/solana'
HISTORY_EVERY_S = 600
MARKET_SKIPS = ('quote', 'age', 'liquidity', 'buy pressure', 'volume', 'price change', 'price')


def load(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def save(p: Path, data) -> None:
    p.write_text(json.dumps(data, indent=1, default=str))


def _get(http, url, params=None):
    try:
        r = http.get(url, params=params, timeout=20, headers={'accept': 'application/json'})
        return r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def candidates(http, now: float) -> list[dict]:
    pools, seen = [], set()
    urls = [(f'{GT}/trending_pools', {'page': p, 'duration': '1h', 'include': 'base_token,quote_token'}) for p in (1, 2)]
    urls += [(f'{GT}/new_pools', {'page': p, 'include': 'base_token,quote_token'}) for p in (1, 2, 3)]
    for url, params in urls:
        for d in (_get(http, url, params) or {}).get('data') or []:
            a, rel = d.get('attributes') or {}, d.get('relationships') or {}
            addr = a.get('address')
            if not addr or addr in seen:
                continue
            seen.add(addr)
            base = ((rel.get('base_token') or {}).get('data') or {}).get('id', '').replace('solana_', '')
            quote = ((rel.get('quote_token') or {}).get('data') or {}).get('id', '').replace('solana_', '')
            try:
                created = datetime.fromisoformat(a['pool_created_at'].replace('Z', '+00:00')).timestamp()
                pools.append({'pool': addr, 'mint': base, 'quote': quote, 'name': a.get('name', ''),
                              'age_s': now - created, 'liq': float(a.get('reserve_in_usd') or 0),
                              'price': float(a.get('base_token_price_usd') or 0),
                              'm5': (a.get('transactions') or {}).get('m5') or {},
                              'vol_h1': float((a.get('volume_usd') or {}).get('h1') or 0),
                              'chg_h1': float((a.get('price_change_percentage') or {}).get('h1') or 0)})
            except (KeyError, TypeError, ValueError):
                continue
    return pools


def market_ok(c: dict) -> str | None:
    """Reason to skip, or None if the cheap market filters pass."""
    m5 = c['m5']
    if c['quote'] not in QUOTES:
        return 'quote'
    if not AGE_MIN_S <= c['age_s'] <= AGE_MAX_S:
        return 'age'
    if c['liq'] < MIN_LIQ:
        return 'liquidity'
    if (m5.get('buys') or 0) < 1.5 * (m5.get('sells') or 0) or (m5.get('buyers') or 0) < 20:
        return 'buy pressure'
    if c['vol_h1'] < 0.5 * c['liq']:
        return 'volume'
    if not 0 <= c['chg_h1'] <= 100:
        return 'price change'
    if c['price'] <= 0:
        return 'price'
    return None


def safety(report: dict | None) -> str | None:
    """Reason to reject from a RugCheck report, or None if safe enough."""
    if not report:
        return 'no rugcheck'
    if any((r.get('level') or '') == 'danger' for r in report.get('risks') or []):
        return 'danger risk'
    tok = report.get('token') or {}
    if tok.get('mintAuthority') or tok.get('freezeAuthority') or report.get('mintAuthority') or report.get('freezeAuthority'):
        return 'authority'
    if ((report.get('transferFee') or {}).get('pct') or 0) > 0:
        return 'transfer fee'
    markets = report.get('markets') or []
    locked = max([((m.get('lp') or {}).get('lpLockedPct') or 0) for m in markets] or [0])
    if locked < 90:
        return 'lp not locked'
    amm = {m.get('pubkey') for m in markets} | {k for k, v in (report.get('knownAccounts') or {}).items()
                                                if (v or {}).get('type') in ('AMM', 'LOCKER')}
    top = sum(h.get('pct') or 0 for h in (report.get('topHolders') or [])[:10]
              if h.get('owner') not in amm and h.get('address') not in amm)
    if top > 30:
        return 'concentrated holders'
    return None


def quotes(http, pools: list[str]) -> dict:
    out = {}
    for i in range(0, len(pools), 30):
        d = _get(http, f"{DS}/{','.join(pools[i:i + 30])}") or {}
        for p in d.get('pairs') or []:
            try:
                out[p['pairAddress']] = {'price': float(p.get('priceUsd') or 0),
                                         'liq': float((p.get('liquidity') or {}).get('usd') or 0)}
            except (TypeError, ValueError, KeyError):
                continue
    return out


def value(p: dict, price: float) -> float:
    """Kronor received if the remaining position were sold at `price`."""
    return p['stake_left'] * price / p['entry'] * (1 - COST)


def exit_all(st: dict, p: dict, price: float, why: str, iso: str) -> None:
    got = value(p, price) if price > 0 else 0.0
    st['cash'] += got
    p['realised'] += got
    p.update(closed=iso, exit_price=price, reason=why, pnl=round(p['realised'] - p['stake'], 2),
             won=p['realised'] > p['stake'], stake_left=0.0)
    st['open'].remove(p)
    st['closed'].append(p)


def step(state_dir: str | Path, now: float | None = None, http=None, log=print) -> dict:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    http = http or requests.Session()
    now = now or time.time()
    path = d / 'memebot_state.json'
    st = load(path, {'started': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'trial': N_TRIAL,
                     'start_kr': START_KR, 'cash': START_KR, 'open': [], 'closed': [], 'seen': [], 'history': [], 'skips': {}})
    iso = datetime.fromtimestamp(now, timezone.utc).isoformat()
    # 1. exits
    q = quotes(http, [p['pool'] for p in st['open']])
    for p in list(st['open']):
        o = q.get(p['pool'])
        if not o or o['price'] <= 0 or (o['liq'] and o['liq'] < 1000):
            p['missing'] = p.get('missing', 0) + 1
            if p['missing'] >= 3:                      # one empty answer is not a rug; three in a row are
                exit_all(st, p, 0.0, 'rug / pair gone', iso)
            continue
        p['missing'] = 0
        px = o['price']
        p['last'], p['peak'] = px, max(p['peak'], px)
        r = px / p['entry']
        if not p['half_sold'] and r >= 2:
            got = value(p, px) / 2
            st['cash'] += got
            p['realised'] += got
            p['stake_left'] /= 2
            p['half_sold'] = True
            p.setdefault('events', []).append({'ts': iso, 'what': 'sold half at 2x', 'kr': round(got, 2)})
        if not p['half_sold'] and r <= 0.6:
            exit_all(st, p, px, 'stop -40 %', iso)
        elif p['half_sold'] and (r >= 5 or px <= 0.5 * p['peak']):
            exit_all(st, p, px, '5x' if r >= 5 else 'trailing -50 % from peak', iso)
        elif now - p['opened_ts'] >= 48 * 3600:
            exit_all(st, p, px, '48 h', iso)
    # 2. entries
    if len(st['open']) < MAX_OPEN and st['cash'] >= STAKE_KR:
        seen = set(st['seen'])
        for c in candidates(http, now):
            if len(st['open']) >= MAX_OPEN or st['cash'] < STAKE_KR:
                break
            if c['mint'] in seen:
                continue
            why = market_ok(c)
            if why is None:
                why = safety(_get(http, f"{RC}/{c['mint']}/report"))
            if why:
                st['skips'][why] = st['skips'].get(why, 0) + 1
                if why not in MARKET_SKIPS:
                    seen.add(c['mint'])                # failed the safety check: never look again
                continue
            seen.add(c['mint'])
            st['cash'] -= STAKE_KR
            st['open'].append({'pool': c['pool'], 'mint': c['mint'], 'name': c['name'], 'opened': iso, 'opened_ts': now,
                               'entry': c['price'] * (1 + COST), 'entry_seen': c['price'], 'stake': STAKE_KR,
                               'stake_left': STAKE_KR, 'peak': c['price'], 'last': c['price'], 'half_sold': False,
                               'realised': 0.0, 'liq_at_entry': c['liq']})
            log(f"meme buy {c['name']} at {c['price']:.3g} USD, liquidity {c['liq']:.0f}")
        st['seen'] = list(seen)[-20000:]
    # 3. equity
    eq = st['cash'] + sum(value(p, p['last']) for p in st['open'])
    row = {'ts': iso, 'equity': round(eq, 2), 'cash': round(st['cash'], 2), 'open': len(st['open']), 'w': int(now // HISTORY_EVERY_S)}
    if st['history'] and st['history'][-1].get('w') == row['w']:
        st['history'][-1] = row
    else:
        st['history'].append(row)
    st['history'] = st['history'][-20000:]
    st['last_run'] = iso
    save(path, st)
    return {'net': round(eq - START_KR, 2), 'avg_share': None, 'open': len(st['open']), 'closed': len(st['closed'])}
