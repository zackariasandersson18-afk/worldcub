"""Scan Polymarket multi-outcome events for riskless baskets against live order books.

In an event whose outcomes are mutually exclusive, exactly one (at most one)
market resolves YES. Two baskets then pay a fixed amount whatever happens:

  ALL-YES  one YES share of every outcome      pays 1        (only if the outcomes
                                                             are also exhaustive:
                                                             temperature ranges are)
  ALL-NO   one NO share of every outcome       pays n - 1    (n outcomes; pays n if
                                                             none resolves YES)

A basket is an arbitrage when buying it at the asks, taker fee included, costs
less than it pays. The scan walks the order books level by level, so the size
is what was really offered, and reports the profit for a given budget.

Not risk-free in practice: the legs are separate orders (one can fill and
another not), a market can be disputed or resolve 50-50, and the money is
locked until resolution.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import requests

GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
DEFAULT_FEE = (0.05, 1.0)   # used when a market publishes no fee schedule: the highest we know
MIN_SHARES = 5.0            # Polymarket orderMinSize


@dataclass
class Leg:
    label: str
    token: str
    fee_rate: float
    fee_exp: float
    asks: list = field(default_factory=list)   # [(price, size)] ascending


@dataclass
class Basket:
    event: str
    slug: str
    kind: str          # 'ALL-YES' or 'ALL-NO'
    payout: float      # per basket
    legs: list


def fee(price: float, rate: float, exp: float) -> float:
    return rate * (price * (1 - price)) ** exp


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def baskets_from_event(e: dict, exhaustive: bool) -> list[Basket]:
    """ALL-NO for every mutually exclusive (negRisk) event; ALL-YES only when exhaustive."""
    ms = [m for m in e.get('markets') or [] if m.get('active', True) and not m.get('closed')]
    if len(ms) < 2 or len(ms) != len(e.get('markets') or []):
        return []   # a closed or missing outcome breaks the basket
    yes, no = [], []
    for m in ms:
        toks = _json(m.get('clobTokenIds')) or []
        if len(toks) != 2:
            return []
        sched = m.get('feeSchedule')
        rate, exp = ((float(sched.get('rate', 0) or 0), float(sched.get('exponent', 1) or 1))
                     if isinstance(sched, dict) else DEFAULT_FEE)
        label = m.get('groupItemTitle') or m.get('question') or '?'
        yes.append(Leg(label, str(toks[0]), rate, exp))
        no.append(Leg(label, str(toks[1]), rate, exp))
    out = [Basket(e.get('title', ''), e.get('slug', ''), 'ALL-NO', len(ms) - 1, no)]
    if exhaustive:
        out.append(Basket(e.get('title', ''), e.get('slug', ''), 'ALL-YES', 1.0, yes))
    return out


def best_fill(b: Basket, budget: float | None = None) -> dict:
    """Buy whole baskets level by level while the next basket still costs less than
    it pays (and the budget lasts). Returns baskets, cost, profit."""
    pos = [0] * len(b.legs)
    left = [b.legs[i].asks[0][1] if b.legs[i].asks else 0.0 for i in range(len(b.legs))]
    q = cost = 0.0
    first_unit = None
    while all(pos[i] < len(b.legs[i].asks) for i in range(len(b.legs))):
        unit = sum(lg.asks[pos[i]][0] + fee(lg.asks[pos[i]][0], lg.fee_rate, lg.fee_exp)
                   for i, lg in enumerate(b.legs))
        if first_unit is None:
            first_unit = unit
        if unit >= b.payout:
            break
        take = min(left)
        if budget is not None:
            take = min(take, (budget - cost) / unit)
        if take <= 1e-9:
            break
        q += take
        cost += take * unit
        for i, lg in enumerate(b.legs):
            left[i] -= take
            if left[i] <= 1e-9:
                pos[i] += 1
                left[i] = lg.asks[pos[i]][1] if pos[i] < len(lg.asks) else 0.0
        if budget is not None and cost >= budget - 1e-6:
            break
    return {'baskets': q, 'cost': cost, 'profit': q * b.payout - cost,
            'first_unit_cost': first_unit, 'executable': q >= MIN_SHARES}


def fetch_events(http, max_pages: int = 30) -> list[tuple[dict, bool]]:
    """Open negRisk (mutually exclusive) events; temperature events are also exhaustive."""
    out, seen = [], set()
    for page in range(max_pages):
        r = http.get(f'{GAMMA}/events', params={'closed': 'false', 'active': 'true', 'limit': 100,
                                                'offset': page * 100, 'order': 'volume24hr',
                                                'ascending': 'false'}, timeout=30)
        if r.status_code == 422:
            break
        r.raise_for_status()
        batch = r.json()
        for e in batch:
            if e.get('id') in seen or not e.get('negRisk'):
                continue
            seen.add(e.get('id'))
            temp = (e.get('title') or '').startswith('Highest temperature in ')
            out.append((e, temp))
        if len(batch) < 100:
            break
    return out


def load_books(baskets: list[Basket], http) -> None:
    legs = {lg.token: lg for b in baskets for lg in b.legs}
    toks = list(legs)
    for i in range(0, len(toks), 100):
        for attempt in range(3):
            try:
                r = http.post(f'{CLOB}/books', json=[{'token_id': t} for t in toks[i:i + 100]], timeout=30)
                r.raise_for_status()
                for book in r.json():
                    lg = legs.get(str(book.get('asset_id')))
                    if lg:
                        lg.asks = sorted((float(a['price']), float(a['size'])) for a in book.get('asks') or [])
                break
            except requests.RequestException:
                time.sleep(2 ** attempt)


def scan(http, budget_usd: float, max_pages: int = 30) -> dict:
    events = fetch_events(http, max_pages)
    baskets = [b for e, ex in events for b in baskets_from_event(e, ex)]
    load_books(baskets, http)
    found = []
    for b in baskets:
        full = best_fill(b)
        if full['baskets'] > 0:
            mine = best_fill(b, budget_usd)
            found.append({'event': b.event, 'slug': b.slug, 'kind': b.kind, 'legs': len(b.legs),
                          'payout': b.payout, 'first_unit_cost': round(full['first_unit_cost'], 4),
                          'max_baskets': round(full['baskets'], 2), 'max_profit_usd': round(full['profit'], 4),
                          'budget_profit_usd': round(mine['profit'], 4), 'budget_cost_usd': round(mine['cost'], 2),
                          'executable': mine['executable']})
    found.sort(key=lambda x: -x['budget_profit_usd'])
    return {'events': len(events), 'temperature_events': sum(ex for _, ex in events),
            'baskets': len(baskets), 'with_books': sum(all(lg.asks for lg in b.legs) for b in baskets),
            'opportunities': found}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='arbbot.scan')
    ap.add_argument('--budget-sek', type=float, default=1000)
    ap.add_argument('--rounds', type=int, default=1)
    ap.add_argument('--every', type=int, default=120, help='seconds between rounds')
    ap.add_argument('--pages', type=int, default=30)
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    http = requests.Session()
    try:
        fx = http.get('https://api.frankfurter.dev/v1/latest', params={'base': 'USD', 'symbols': 'SEK'},
                      timeout=20).json()['rates']['SEK']
    except Exception:
        fx = 9.4
    budget = a.budget_sek / fx
    print(f'budget {a.budget_sek:.0f} SEK = {budget:.2f} USD (1 USD = {fx:.3f} SEK)')
    rounds = []
    for k in range(a.rounds):
        t0 = time.time()
        res = scan(http, budget, a.pages)
        res['ts'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        rounds.append(res)
        ops = res['opportunities']
        exe = [o for o in ops if o['executable']]
        print(f"\n[{res['ts']}] round {k + 1}: {res['events']} events ({res['temperature_events']} temperature), "
              f"{res['baskets']} baskets, {res['with_books']} with full books, {len(ops)} arbitrages "
              f"({len(exe)} at >= {MIN_SHARES:.0f} shares) in {time.time() - t0:.0f}s")
        for o in ops[:12]:
            print(f"  {o['kind']:7s} legs={o['legs']:2d} cost/basket={o['first_unit_cost']:.4f} pays={o['payout']:.0f} "
                  f"max={o['max_baskets']:.1f} baskets profit=${o['max_profit_usd']:.2f} | "
                  f"my budget: ${o['budget_profit_usd']:.2f} ({o['budget_profit_usd'] * fx:.1f} SEK) "
                  f"{'OK' if o['executable'] else 'below min size'} | {o['event'][:60]}")
        if k + 1 < a.rounds:
            time.sleep(max(0, a.every - (time.time() - t0)))
    if a.out:
        with open(a.out, 'w') as f:
            json.dump({'fx': fx, 'budget_usd': budget, 'rounds': rounds}, f, indent=1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
