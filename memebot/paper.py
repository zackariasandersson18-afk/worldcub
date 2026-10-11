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

Fills from Jupiter (amended 11 Oct 2026, before the bot had made a single trade): instead of a flat
2 % per side, every buy and sell is priced with Jupiter's real quote for exactly that size (lite-api.jup.ag,
USDC in/out, 10 kr per USD), minus 0.5 % for the price moving before the swap lands and 2 kr network +
priority fee per swap. A buy without a route is skipped; an exit without a route is retried every minute
and after three minutes without any route the position is worth 0 (it cannot be sold). If Jupiter does
not answer at all, the 2 % estimate is used and the trade is marked estimated. Entry = USD actually paid per
token, so the 2x / -40 % levels are measured from the real fill like before.

Manual close (added on request, not part of the pre-registered rules): the owner can close an
open position by hand; it is sold at the current price with the same 2 % cost and marked
reason "manuellt stängd" / manual=True, so test 66 can be judged with or without them.
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
JUP = 'https://lite-api.jup.ag/swap/v1/quote'
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
KR_PER_USD = 10.0                                  # same fixed rate as the rewards pages
FEE_KR = 2.0                                       # network + priority fee per swap (about 0.2 USD)
LATENCY = 0.005                                    # 0.5 % worse than the quote: the price moves before the swap lands
HISTORY_EVERY_S = 600
TRAIL_MAX = 2880                                   # one price per minute for 48 h (dashboard chart per trade)
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


def jup(http, in_mint: str, out_mint: str, amount: int):
    """Jupiter's best route for an exact-in swap: ('ok', out_raw, impact) | ('noroute', 0, None) | ('down', 0, None).
    'noroute' means Jupiter answered and nobody will take the trade (unsellable); 'down' means no answer."""
    if amount <= 0:
        return 'noroute', 0, None
    try:
        r = http.get(JUP, params={'inputMint': in_mint, 'outputMint': out_mint, 'amount': str(int(amount)),
                                  'slippageBps': 1500, 'swapMode': 'ExactIn'}, timeout=20, headers={'accept': 'application/json'})
        b = r.json()
    except (requests.RequestException, ValueError):
        return 'down', 0, None
    if r.status_code == 200 and int(b.get('outAmount') or 0) > 0:
        return 'ok', int(b['outAmount']), float(b.get('priceImpactPct') or 0)
    if r.status_code in (400, 404) and isinstance(b, dict) and b.get('errorCode'):
        return 'noroute', 0, None
    return 'down', 0, None


def ui(p: dict, raw: int | None = None) -> float:
    return (p['tokens_left'] if raw is None else raw) / 10 ** p['decimals']


def sell_quote(http, p: dict, raw: int):
    """Kronor received for selling `raw` token units now: (status, kr, impact). Includes the latency haircut and fee."""
    status, out, imp = jup(http, p['mint'], USDC, raw)
    if status != 'ok':
        return status, 0.0, None
    return 'ok', max(0.0, out / 1e6 * (1 - LATENCY) * KR_PER_USD - FEE_KR), imp


def value(p: dict, price: float) -> float:
    """Kronor received if the remaining position were sold at `price`: the last Jupiter sell quote scaled to
    the price, or a 2 % estimate when there is no quote yet (and for positions opened before Jupiter)."""
    if 'tokens_left' not in p:
        return p['stake_left'] * price / p['entry'] * (1 - COST)
    if p.get('quote_kr') is not None and p.get('quote_px'):
        return p['quote_kr'] * price / p['quote_px']
    return max(0.0, ui(p) * price * KR_PER_USD * (1 - COST) - FEE_KR)


def track(p: dict, ts: float, price: float) -> None:
    """Price trail [unix s, USD] for the dashboard's per-trade chart; display only, never read by the rules."""
    t = p.setdefault('trail', [])
    t.append([int(ts), float(f'{price:.6g}')])
    del t[:-TRAIL_MAX]


def exit_all(st: dict, p: dict, price: float, why: str, iso: str, got: float | None = None) -> None:
    if len(p.get('trail') or []) > 300:              # keep closed trades small: every n-th point plus the last
        t = p['trail']
        p['trail'] = t[::len(t) // 250 + 1] + [t[-1]]
    if got is None:
        got = value(p, price) if price > 0 else 0.0
    st['cash'] += got
    p['realised'] += got
    p.update(closed=iso, exit_price=price, reason=why, pnl=round(p['realised'] - p['stake'], 2),
             won=p['realised'] > p['stake'], stake_left=0.0)
    if 'tokens_left' in p:
        p['tokens_left'] = 0
    p.pop('exit_wait', None)
    st['open'].remove(p)
    st['closed'].append(p)


def step(state_dir: str | Path, now: float | None = None, http=None, log=print, close: set | None = None) -> dict:
    """close: pool addresses the owner asked to close by hand (sold at the current price this minute)."""
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
                exit_all(st, p, 0.0, 'rug / pair gone', iso, got=0.0)
            continue
        p['missing'] = 0
        px = o['price']
        track(p, now, px)
        p['last'], p['peak'] = px, max(p['peak'], px)
        if o['liq']:
            p['liq_now'] = o['liq']                    # liquidity in USD right now (shown beside the one at entry)
        jq = None                                      # this minute's Jupiter quote for selling everything
        if 'tokens_left' in p:
            jq = sell_quote(http, p, p['tokens_left'])
            if jq[0] == 'ok':
                p.update(quote_kr=round(jq[1], 4), quote_px=px, exit_impact=jq[2], quote_ts=iso, nosell=0)
            elif jq[0] == 'noroute':
                p['nosell'] = p.get('nosell', 0) + 1
                p.update(quote_kr=0.0, quote_px=px)
        def out(why: str) -> None:
            """Sell everything; with Jupiter, at its quote. No route three minutes in a row = unsellable = worth 0."""
            if jq is None or jq[0] == 'down':          # legacy position, or Jupiter silent: 2 % estimate
                exit_all(st, p, px, why, iso)
                if jq is not None:
                    p['estimated'] = True
            elif jq[0] == 'ok':
                exit_all(st, p, px, why, iso, got=jq[1])
            elif p['nosell'] >= 3:
                exit_all(st, p, px, 'ingen säljväg (kan inte säljas)', iso, got=0.0)
            else:
                p['exit_wait'] = why                   # try again next minute whatever the price does
        if p.get('exit_wait'):
            out(p['exit_wait'])
            continue
        if close and p['pool'] in close:
            p['manual'] = True
            out('manuellt stängd')
            continue
        r = px / p['entry']
        if not p['half_sold'] and r >= 2:
            if 'tokens_left' in p:
                half = p['tokens_left'] // 2
                hs, got, imp = sell_quote(http, p, half)
                if hs == 'down':
                    got, imp = value(p, px) / 2, None
            else:
                hs, got, imp, half = 'legacy', value(p, px) / 2, None, 0
            if hs != 'noroute':                        # no buyer for half: try again next minute
                st['cash'] += got
                p['realised'] += got
                p['stake_left'] /= 2
                p['half_sold'] = True
                if 'tokens_left' in p:
                    p['tokens_left'] -= half
                    p.pop('quote_kr', None)            # the quote was for the whole bag
                p.setdefault('events', []).append({'ts': iso, 'what': 'sold half at 2x', 'kr': round(got, 2), 'impact': imp})
                if 'tokens_left' in p:
                    jq = sell_quote(http, p, p['tokens_left'])
                    if jq[0] == 'ok':
                        p.update(quote_kr=round(jq[1], 4), quote_px=px, exit_impact=jq[2], quote_ts=iso)
        if not p['half_sold'] and r <= 0.6:
            out('stop -40 %')
        elif p['half_sold'] and (r >= 5 or px <= 0.5 * p['peak']):
            out('5x' if r >= 5 else 'trailing -50 % from peak')
        elif now - p['opened_ts'] >= 48 * 3600:
            out('48 h')
    # 2. entries
    if len(st['open']) < MAX_OPEN and st['cash'] >= STAKE_KR:
        seen = set(st['seen'])
        for c in candidates(http, now):
            if len(st['open']) >= MAX_OPEN or st['cash'] < STAKE_KR:
                break
            if c['mint'] in seen:
                continue
            why, report = market_ok(c), None
            if why is None:
                report = _get(http, f"{RC}/{c['mint']}/report")
                why = safety(report)
            dec = ((report or {}).get('token') or {}).get('decimals')
            if why is None and not isinstance(dec, int):
                why = 'no decimals'
            buy = None
            if why is None:                            # a real buy: what Jupiter gives for 250 kr minus the fee
                buy = jup(http, USDC, c['mint'], int((STAKE_KR - FEE_KR) / KR_PER_USD * 1e6))
                if buy[0] != 'ok':
                    why = 'no route' if buy[0] == 'noroute' else 'jupiter down'
            if why:
                st['skips'][why] = st['skips'].get(why, 0) + 1
                if why not in MARKET_SKIPS + ('jupiter down',):
                    seen.add(c['mint'])                # failed the safety check: never look again
                continue
            seen.add(c['mint'])
            tokens = int(buy[1] * (1 - LATENCY))
            entry = STAKE_KR / KR_PER_USD / (tokens / 10 ** dec)   # USD per token actually paid, all costs in
            st['cash'] -= STAKE_KR
            st['open'].append({'pool': c['pool'], 'mint': c['mint'], 'name': c['name'], 'opened': iso, 'opened_ts': now,
                               'entry': entry, 'entry_seen': c['price'], 'stake': STAKE_KR,
                               'stake_left': STAKE_KR, 'peak': c['price'], 'last': c['price'], 'half_sold': False,
                               'realised': 0.0, 'liq_at_entry': c['liq'], 'trail': [[int(now), float(f"{c['price']:.6g}")]],
                               'tokens': tokens, 'tokens_left': tokens, 'decimals': dec, 'entry_impact': buy[2],
                               'entry_cost': round(entry / c['price'] - 1, 4) if c['price'] else None, 'fills': 'jupiter'})
            log(f"meme buy {c['name']} at {entry:.3g} USD (screen {c['price']:.3g}), liquidity {c['liq']:.0f}")
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
