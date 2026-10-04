"""Live test of ImMike/polymarket-arbitrage (cross-platform Polymarket <-> Kalshi).

The repo's headline numbers ("99.6 % win rate, $573 profit") come from its
simulation mode, which per its own README "generates fake data with
opportunities". Arbitrage needs simultaneous order books on both venues, which
have no public history, so it is tested live here, read-only:

  1. Polymarket: active binary markets from Gamma (top by 24 h volume) with best
     bid/ask; Kalshi: open markets from its public API (multivariate combos excluded)
  2. pairs are found with the repo's OWN matcher (core/cross_platform_arb.py,
     MarketMatcher, min_similarity 0.6 as in its config.yaml)
  3. each pair is priced in all four directions two ways:
       theirs     the repo's check_arbitrage formula and fee constants
                  (Polymarket 1.5 %, Kalshi 1 %, 0.02 gas per leg, min edge 2 %)
       realistic  buy YES on one venue + NO on the other at the asks; Polymarket
                  taker fee from the market's feeSchedule, Kalshi taker fee
                  0.07 * P * (1 - P) per contract; profit = 1 - total cost
  4. bundle arbitrage inside one Polymarket binary market (YES ask + NO ask < 1)
     is checked against the CLOB books of the matched markets

A positive edge is only an arbitrage if both markets resolve on the same event;
the report prints both titles so that can be judged.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from types import SimpleNamespace

import requests

GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
KALSHI = 'https://api.elections.kalshi.com/trade-api/v2'


def _get(http, url, params=None, tries=4):
    for i in range(tries):
        try:
            r = http.get(url, params=params, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.RequestException(r.status_code)
            if r.status_code >= 400:
                return None
            return r.json()
        except requests.RequestException:
            time.sleep(2 ** i)
    return None


def _j(v):
    return json.loads(v) if isinstance(v, str) else v


def polymarket_markets(http, pages=10):
    out = []
    for page in range(pages):
        batch = _get(http, f'{GAMMA}/markets', {'active': 'true', 'closed': 'false', 'limit': 500,
                                                'offset': page * 500, 'order': 'volume24hr', 'ascending': 'false'})
        if not batch:
            break
        for m in batch:
            outs = _j(m.get('outcomes')) or []
            toks = _j(m.get('clobTokenIds')) or []
            if [str(o).lower() for o in outs] != ['yes', 'no'] or len(toks) != 2:
                continue
            bid, ask = m.get('bestBid'), m.get('bestAsk')
            if bid is None or ask is None:
                continue
            sched = m.get('feeSchedule') or {}
            out.append(SimpleNamespace(market_id=str(m['id']), question=m.get('question') or '', active=True,
                                       yes_bid=float(bid), yes_ask=float(ask), yes_token=str(toks[0]),
                                       no_token=str(toks[1]), slug=m.get('slug'),
                                       fee_rate=float(sched.get('rate', 0) or 0),
                                       fee_exp=float(sched.get('exponent', 1) or 1)))
        if len(batch) < 500:
            break
    return out


def _cents(m, key):
    v = m.get(f'{key}_dollars')
    if v not in (None, ''):
        return float(v)
    v = m.get(key)
    return None if v in (None, '') else float(v) / 100


def kalshi_markets(http, pages=15, log=print):
    out, cursor = [], None
    for _ in range(pages):
        params = {'status': 'open', 'limit': 1000, 'mve_filter': 'exclude'}
        if cursor:
            params['cursor'] = cursor
        d = _get(http, f'{KALSHI}/markets', params)
        if not d:
            break
        if not out and d.get('markets'):
            log('kalshi fields: ' + ', '.join(sorted(d['markets'][0].keys()))[:600])
        for m in d.get('markets') or []:
            yb, ya, nb, na = (_cents(m, k) for k in ('yes_bid', 'yes_ask', 'no_bid', 'no_ask'))
            if not ya or not yb or ya >= 1 or yb <= 0:
                continue
            title = m.get('title') or ''
            sub = m.get('yes_sub_title') or m.get('subtitle') or ''
            out.append(SimpleNamespace(ticker=m.get('ticker'), title=f'{title} {sub}'.strip(), is_active=True,
                                       yes_bid=yb, yes_ask=ya, no_bid=nb if nb else 1 - ya,
                                       no_ask=na if na else 1 - yb, rules=(m.get('rules_primary') or '')[:300]))
        cursor = d.get('cursor')
        if not cursor:
            break
    return out


def poly_fee(p, m):
    return m.fee_rate * (p * (1 - p)) ** m.fee_exp


def kalshi_fee(p):
    return 0.07 * p * (1 - p)


def price_pair(pm, km):
    """Realistic: both ways of holding YES on one venue and NO on the other."""
    legs = [('YES@Polymarket + NO@Kalshi', pm.yes_ask, poly_fee(pm.yes_ask, pm), km.no_ask, kalshi_fee(km.no_ask)),
            ('NO@Polymarket + YES@Kalshi', 1 - pm.yes_bid, poly_fee(1 - pm.yes_bid, pm), km.yes_ask, kalshi_fee(km.yes_ask))]
    best = None
    for name, a, fa, b, fb in legs:
        edge = 1 - (a + fa + b + fb)
        if best is None or edge > best['edge']:
            best = {'direction': name, 'cost': round(a + fa + b + fb, 4), 'edge': round(edge, 4),
                    'raw_cost': round(a + b, 4)}
    return best


def theirs(pm, km, min_edge=0.02, pf=0.015, kf=0.01, gas=0.02):
    """The repo's check_arbitrage arithmetic (sell = buy the opposite token)."""
    pna, pnb = 1 - pm.yes_bid, 1 - pm.yes_ask
    dirs = [(km.yes_bid - pm.yes_ask, pm.yes_ask * pf + km.yes_bid * kf),
            (pm.yes_bid - km.yes_ask, km.yes_ask * kf + pm.yes_bid * pf),
            (km.no_bid - pna, pna * pf + km.no_bid * kf),
            (pnb - km.no_ask, km.no_ask * kf + pnb * pf)]
    best = max(g - f - 2 * gas for g, f in dirs)
    return round(best, 4), best >= min_edge


def run_round(repo: str, http, log=print) -> dict:
    sys.path.insert(0, repo)
    from core.cross_platform_arb import MarketMatcher   # the repo's own matcher
    import logging
    logging.disable(logging.INFO)
    pms, kms = polymarket_markets(http), kalshi_markets(http, log=log)
    log(f'{len(pms)} Polymarket binary markets, {len(kms)} Kalshi markets')
    t0 = time.time()
    pairs = asyncio.run(MarketMatcher(min_similarity=0.6).find_matches(pms, kms))
    log(f'{len(pairs)} pairs matched by their matcher in {time.time() - t0:.0f}s')
    pmap = {m.market_id: m for m in pms}
    kmap = {m.ticker: m for m in kms}
    rows = []
    for p in pairs:
        pm, km = pmap[p.polymarket_id], kmap[p.kalshi_ticker]
        t_edge, t_signal = theirs(pm, km)
        real = price_pair(pm, km)
        rows.append({'poly': pm.question, 'kalshi': km.title, 'score': round(p.similarity_score, 3),
                     'category': p.category, 'poly_yes': [pm.yes_bid, pm.yes_ask],
                     'kalshi_yes': [km.yes_bid, km.yes_ask], 'their_edge': t_edge, 'their_signal': t_signal,
                     **{f'real_{k}': v for k, v in real.items()}, 'kalshi_rules': km.rules, 'poly_slug': pm.slug})
    rows.sort(key=lambda r: -r['real_edge'])
    # bundle check on the matched Polymarket markets, against real CLOB books
    bundle = []
    toks = [(pmap[p.polymarket_id].yes_token, pmap[p.polymarket_id].no_token) for p in pairs][:200]
    flat = [t for pair in toks for t in pair]
    asks = {}
    for i in range(0, len(flat), 100):
        try:
            r = http.post(f'{CLOB}/books', json=[{'token_id': t} for t in flat[i:i + 100]], timeout=30)
            for b in r.json():
                a = [float(x['price']) for x in b.get('asks') or []]
                asks[str(b.get('asset_id'))] = min(a) if a else None
        except Exception:
            pass
    for y, n in toks:
        if asks.get(y) is not None and asks.get(n) is not None:
            bundle.append(round(asks[y] + asks[n], 4))
    return {'polymarket': len(pms), 'kalshi': len(kms), 'pairs': len(pairs),
            'their_signals': sum(r['their_signal'] for r in rows),
            'real_positive': sum(r['real_edge'] > 0 for r in rows),
            'bundle_checked': len(bundle), 'bundle_min_total_ask': min(bundle) if bundle else None,
            'bundle_below_1': sum(b < 1 for b in bundle), 'top': rows[:25]}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='arbbot.cross_kalshi')
    ap.add_argument('--repo', required=True, help='path to a clone of ImMike/polymarket-arbitrage')
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--every', type=int, default=300)
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    http = requests.Session()
    out = []
    for k in range(a.rounds):
        t0 = time.time()
        res = run_round(a.repo, http)
        res['ts'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        out.append(res)
        print(f"\n[{res['ts']}] round {k + 1}: pairs={res['pairs']} their_signals={res['their_signals']} "
              f"realistic_positive={res['real_positive']} bundle: {res['bundle_below_1']}/{res['bundle_checked']} "
              f"below 1 (min {res['bundle_min_total_ask']})")
        for r in res['top'][:15]:
            print(f"  real {r['real_edge']:+.4f} ({r['real_direction']}, raw {r['real_raw_cost']}) theirs {r['their_edge']:+.4f}"
                  f"{' SIGNAL' if r['their_signal'] else ''} score {r['score']}\n"
                  f"     P: {r['poly'][:90]}  {r['poly_yes']}\n     K: {r['kalshi'][:90]}  {r['kalshi_yes']}")
        if k + 1 < a.rounds:
            time.sleep(max(0, a.every - (time.time() - t0)))
    if a.out:
        with open(a.out, 'w') as f:
            json.dump(out, f, indent=1, default=str)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
