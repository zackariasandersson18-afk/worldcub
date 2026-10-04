"""Live forward measurement of liquidity-rewards market making (virtual, read-only).

The back-test (rewardbot.backtest) could not verify three things; this measures
them going forward, every tick (~30 min):
  1. competition: competitors' Qmin from the live YES/NO books of each chosen market
  2. our estimated reward: rate_per_day * our Qmin share, accrued over the time since
     the last tick (0 while the midpoint is outside [0.10, 0.90])
  3. adverse selection: virtual quotes at mid +/- v/2 (rewards_min_size shares, the
     back-test's d = v/2 variant) filled by real trades strictly through them since
     the last tick; inventory capped at 3 x min size; marked to the current midpoint
It also identifies the reward token once via Polygon JSON-RPC (symbol, decimals).

The portfolio is chosen once, on the first run, by the back-test's pre-registered
rule (1000 USD, best estimated reward per dollar of collateral) and then frozen, so
the measurement is not refitted to what it sees. Rewards are only paid to real
resting orders: these numbers are estimates of what would have been earned.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from rewardbot import backtest as bt

POLYGON_RPCS = ('https://polygon-rpc.com', 'https://polygon-bor-rpc.publicnode.com', 'https://1rpc.io/matic')
USDC_E = '0x2791bca1f2de4661ed88a30c99a7a9449aa84174'


def load(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def save(p: Path, data) -> None:
    p.write_text(json.dumps(data, indent=1, default=str))


def token_info(address: str, http) -> dict:
    """symbol() and decimals() of an ERC-20 on Polygon, via public JSON-RPC."""
    def call(rpc, data):
        r = http.post(rpc, json={'jsonrpc': '2.0', 'id': 1, 'method': 'eth_call',
                                 'params': [{'to': address, 'data': data}, 'latest']}, timeout=20)
        return r.json().get('result')
    for rpc in POLYGON_RPCS:
        try:
            sym_hex, dec_hex = call(rpc, '0x95d89b41'), call(rpc, '0x313ce567')
            if not sym_hex or sym_hex == '0x':
                continue
            raw = bytes.fromhex(sym_hex[2:])
            if len(raw) >= 96:                       # ABI-encoded string
                n = int.from_bytes(raw[32:64], 'big')
                symbol = raw[64:64 + n].decode(errors='replace')
            else:
                symbol = raw.rstrip(b'\0').decode(errors='replace')
            return {'address': address, 'symbol': symbol, 'decimals': int(dec_hex, 16), 'rpc': rpc}
        except Exception:
            continue
    return {'address': address, 'symbol': None, 'error': 'no RPC answered'}


def choose(http, log) -> list[dict]:
    cands = bt.candidates(http, log)
    rows = []
    for c in cands:
        d = round(c['v'] / 200, 4)
        rew = bt.expected_reward(c['rate'], c['v'], c['min_size'], d * 100, c['rest_q'])
        coll = c['min_size'] * (c['mid'] - d) + c['min_size'] * (1 - c['mid'] - d)
        rows.append({**c, 'd': d, 'est_reward_day': rew, 'collateral': coll,
                     'per_dollar': rew / coll if coll > 0 else 0})
    rows.sort(key=lambda r: -r['per_dollar'])
    chosen, used = [], 0.0
    for r in rows:
        if r['est_reward_day'] > 0 and used + r['collateral'] <= bt.CAPITAL:
            chosen.append({k: r[k] for k in ('cond', 'question', 'yes', 'no', 'rate', 'v', 'min_size', 'd',
                                             'asset', 'collateral', 'est_reward_day', 'end')})
            used += r['collateral']
    log(f'portfolio frozen: {len(chosen)} markets, collateral {used:.2f} USD, '
        f'estimated {sum(c["est_reward_day"] for c in chosen):.2f}/day at selection')
    return chosen


def step(state_dir: str | Path, now: float | None = None, http=None, log=print) -> dict:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    http = http or requests.Session()
    now = now or time.time()
    st = load(d / 'rewards_state.json', {})
    if not st.get('portfolio'):
        st = {'started': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'portfolio': choose(http, log),
              'positions': {}, 'history': [], 'reward_total': 0.0, 'last_ts': now, 'tokens': {}}
    for c in st['portfolio']:
        a = (c.get('asset') or '').lower()
        if a and a not in st['tokens']:
            st['tokens'][a] = token_info(c['asset'], http) if a != USDC_E else {'symbol': 'USDC.e', 'decimals': 6}
    dt = max(0.0, now - st['last_ts'])
    books = bt.fetch_books([c[k] for c in st['portfolio'] for k in ('yes', 'no')], http)
    tick = {'ts': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'dt_h': round(dt / 3600, 3), 'markets': []}
    reward_tick = mm_value = 0.0
    for c in st['portfolio']:
        pos = st['positions'].setdefault(c['cond'], {'inv': 0.0, 'cash': 0.0, 'mid': None, 'reward': 0.0, 'fills': 0})
        yb, nb = books.get(c['yes']), books.get(c['no'])
        if not yb or not yb.get('bids') or not yb.get('asks'):
            tick['markets'].append({'q': c['question'][:50], 'status': 'no book'})
            continue
        mid = (max(float(x['price']) for x in yb['bids']) + min(float(x['price']) for x in yb['asks'])) / 2
        rest = bt.book_q(yb, nb or {}, mid, c['v'], c['min_size'])
        share = bt.expected_reward(1.0, c['v'], c['min_size'], c['d'] * 100, rest)
        earned = c['rate'] * share * dt / 86400 if 0.10 <= mid <= 0.90 and dt > 0 else 0.0
        # fills since the last tick against the quotes that were resting (previous mid +/- d)
        if pos['mid'] is not None and dt > 0:
            trades = bt.fetch_trades(c['cond'], c['yes'], int(st['last_ts']), http)
            bid, ask, cap = pos['mid'] - c['d'], pos['mid'] + c['d'], bt.INV_CAP_MULT * c['min_size']
            bq = c['min_size'] if pos['inv'] + c['min_size'] <= cap else 0.0
            aq = c['min_size'] if -pos['inv'] + c['min_size'] <= cap else 0.0
            for t, p, z in trades:
                if t < st['last_ts'] or t >= now:
                    continue
                if bq > 0 and p < bid:
                    q = min(bq, z); bq -= q; pos['inv'] += q; pos['cash'] -= q * bid; pos['fills'] += 1   # noqa: E702
                elif aq > 0 and p > ask:
                    q = min(aq, z); aq -= q; pos['inv'] -= q; pos['cash'] += q * ask; pos['fills'] += 1   # noqa: E702
        pos['mid'] = mid
        pos['reward'] += earned
        reward_tick += earned
        value = pos['cash'] + pos['inv'] * mid
        mm_value += value
        tick['markets'].append({'q': c['question'][:50], 'mid': round(mid, 3), 'rest_q': [round(x, 1) for x in rest],
                                'share': round(share, 4), 'earned': round(earned, 4), 'inv': pos['inv'],
                                'mm_value': round(value, 2)})
    st['reward_total'] += reward_tick
    st['last_ts'] = now
    tick.update(reward_tick=round(reward_tick, 4), reward_total=round(st['reward_total'], 2),
                mm_value=round(mm_value, 2), net=round(st['reward_total'] + mm_value, 2),
                avg_share=round(sum(m.get('share', 0) for m in tick['markets']) / max(len(tick['markets']), 1), 4),
                fills_total=sum(p.get('fills', 0) for p in st['positions'].values()))
    st['history'].append({k: tick[k] for k in ('ts', 'dt_h', 'reward_tick', 'reward_total', 'mm_value', 'net',
                                               'avg_share', 'fills_total')})
    st['history'] = st['history'][-3000:]
    st['last_tick'] = tick
    save(d / 'rewards_state.json', st)
    return tick


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='rewardbot.live')
    ap.add_argument('--state', default='state/rewards')
    a = ap.parse_args(argv)
    t = step(a.state)
    print(json.dumps({k: t[k] for k in ('ts', 'dt_h', 'reward_tick', 'reward_total', 'mm_value', 'net', 'avg_share')}))
    for m in t['markets'][:30]:
        print(' ', m)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
