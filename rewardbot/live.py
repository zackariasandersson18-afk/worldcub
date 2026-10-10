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

Alongside it runs a REALISTIC ledger (added 2026-10-05, rules fixed before any
result was seen). It answers "what if real money had been resting there", as far
as public data allows:
  R1 competition  the interval's share is the LOWER of the share at the previous
                  tick and now (competitors may have been there all along)
  R2 band         reward only if the midpoint was in [0.10, 0.90] at both ticks
  R3 fast market  no reward for the interval if the midpoint moved more than
                  v - d cents between ticks (the quote would have left the scoring
                  range or been pulled while being re-placed)
  R4 queue        a trade AT our price fills us too (we are assumed last in the
                  queue only for the good fills: touches usually come before a move
                  through, so this adds adverse selection)
  R5 no book      a market whose book is gone (closed/resolving) keeps its last
                  value instead of dropping out of the sum (fixed in both ledgers)
Not modelled: bot downtime, order-placement latency, the $1 minimum payout.

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


def refresh_rates(portfolio: list[dict], http, log=print) -> None:
    """Rewards are paid at today's rate_per_day, not the one seen when the market was
    chosen. A market no longer in a successful listing earns nothing (rate 0); if the
    listing fails, the rates are left as they were."""
    try:
        rm = bt.reward_markets(http, log)
    except Exception as exc:                                  # noqa: BLE001
        log(f'rate refresh failed: {exc}')
        return
    if not rm:
        return
    now = {m['condition_id']: float(sum(c.get('rate_per_day', 0) for c in m.get('rewards_config') or [])) for m in rm}
    for c in portfolio:
        c.setdefault('rate0', c['rate'])
        c['rate'] = now.get(c['cond'], 0.0)


def fill_row(t: int, c: dict, side: str, price: float, size: float, ledger: str) -> dict:
    """One virtual fill of the YES token at our quote."""
    return {'ts': datetime.fromtimestamp(t, timezone.utc).isoformat(), 'cond': c['cond'], 'q': c['question'][:80],
            'side': side, 'price': round(price, 4), 'size': size, 'ledger': ledger}


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


def backfill_real(st: dict) -> None:
    """One-off: the realistic ledger started later than the measurement. Fill the gap
    from the tick history, which only has portfolio-average shares: R1 is applied to the
    average share (reward_tick * min(previous, current) / current); R2, R3 and R4 cannot
    be checked without per-market data, so market making is taken from the optimistic
    ledger. Marked as an estimate in st['real_backfill']."""
    h = st.get('history', [])
    first = next((i for i, x in enumerate(h) if x.get('real_net') is not None), None)
    if st.get('real_backfill') or not first:
        return
    rew, prev = 0.0, None
    for x in h[:first + 1]:
        if prev is not None and x['avg_share'] > 0 and x.get('real_net') is None:
            rew += x['reward_tick'] * min(prev, x['avg_share']) / x['avg_share']
        elif prev is not None and x.get('real_net') is not None:
            # the ledger's own first tick earned nothing (no previous share yet): fill it too
            rew += x['reward_tick'] * min(prev, x['avg_share']) / x['avg_share']
        prev = x['avg_share']
        if x.get('real_net') is None:
            x.update(real_reward_total=round(rew, 2), real_mm_value=x['mm_value'],
                     real_net=round(rew + x['mm_value'], 2), real_fills_total=x.get('fills_total', 0))
    mm0, fills0 = h[first]['mm_value'], h[first].get('fills_total', 0)
    for x in h[first:]:
        x['real_reward_total'] = round(x['real_reward_total'] + rew, 2)
        x['real_mm_value'] = round(x['real_mm_value'] + mm0, 2)
        x['real_net'] = round(x['real_reward_total'] + x['real_mm_value'], 2)
        x['real_fills_total'] = x.get('real_fills_total', 0) + fills0
    st['real_reward_total'] = st.get('real_reward_total', 0.0) + rew
    st['real_backfill'] = {'until': h[first]['ts'], 'reward': round(rew, 4), 'mm_value': mm0, 'fills': fills0,
                           'note': 'estimate: R1 on the average share, optimistic market making'}
    st['real_started'] = st['started']


def step(state_dir: str | Path, now: float | None = None, http=None, log=print) -> dict:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    http = http or requests.Session()
    now = now or time.time()
    st = load(d / 'rewards_state.json', {})
    backfill_real(st)
    if not st.get('portfolio'):
        st = {'started': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'portfolio': choose(http, log),
              'positions': {}, 'history': [], 'reward_total': 0.0, 'last_ts': now, 'tokens': {}}
    for c in st['portfolio']:
        a = (c.get('asset') or '').lower()
        if a and a not in st['tokens']:
            st['tokens'][a] = token_info(c['asset'], http) if a != USDC_E else {'symbol': 'USDC.e', 'decimals': 6}
    dt = max(0.0, now - st['last_ts'])
    refresh_rates(st['portfolio'], http, log)
    books = bt.fetch_books([c[k] for c in st['portfolio'] for k in ('yes', 'no')], http)
    tick = {'ts': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'dt_h': round(dt / 3600, 3), 'markets': []}
    reward_tick = mm_value = r_tick = r_mm = 0.0
    fills = st.setdefault('fills', [])           # every virtual fill, both ledgers (kept from 2026-10-05 on)
    for c in st['portfolio']:
        pos = st['positions'].setdefault(c['cond'], {'inv': 0.0, 'cash': 0.0, 'mid': None, 'reward': 0.0, 'fills': 0})
        for k, v in (('r_inv', 0.0), ('r_cash', 0.0), ('r_fills', 0), ('r_reward', 0.0)):
            pos.setdefault(k, v)
        yb, nb = books.get(c['yes']), books.get(c['no'])
        if not yb or not yb.get('bids') or not yb.get('asks'):
            last = pos['mid'] or 0.0                       # R5: keep the last known value
            mm_value += pos['cash'] + pos['inv'] * last
            r_mm += pos['r_cash'] + pos['r_inv'] * last
            pos['share'] = None
            tick['markets'].append({'q': c['question'][:50], 'status': 'no book'})
            continue
        mid = (max(float(x['price']) for x in yb['bids']) + min(float(x['price']) for x in yb['asks'])) / 2
        rest = bt.book_q(yb, nb or {}, mid, c['v'], c['min_size'])
        share = bt.expected_reward(1.0, c['v'], c['min_size'], c['d'] * 100, rest)
        earned = c['rate'] * share * dt / 86400 if 0.10 <= mid <= 0.90 and dt > 0 else 0.0
        # realistic ledger: R1-R3
        prev_mid, prev_share = pos['mid'], pos.get('share')
        r_earned = 0.0
        if (dt > 0 and prev_mid is not None and prev_share is not None and 0.10 <= mid <= 0.90
                and 0.10 <= prev_mid <= 0.90 and abs(mid - prev_mid) * 100 <= c['v'] - c['d'] * 100):
            r_earned = c['rate'] * min(share, prev_share) * dt / 86400
        # fills since the last tick against the quotes that were resting (previous mid +/- d)
        if pos['mid'] is not None and dt > 0:
            trades = bt.fetch_trades(c['cond'], c['yes'], int(st['last_ts']), http)
            bid, ask, cap = pos['mid'] - c['d'], pos['mid'] + c['d'], bt.INV_CAP_MULT * c['min_size']
            bq = c['min_size'] if pos['inv'] + c['min_size'] <= cap else 0.0
            aq = c['min_size'] if -pos['inv'] + c['min_size'] <= cap else 0.0
            rbq = c['min_size'] if pos['r_inv'] + c['min_size'] <= cap else 0.0
            raq = c['min_size'] if -pos['r_inv'] + c['min_size'] <= cap else 0.0
            eps = 1e-9
            for t, p, z in trades:
                if t < st['last_ts'] or t >= now:
                    continue
                if bq > 0 and p < bid:
                    q = min(bq, z); bq -= q; pos['inv'] += q; pos['cash'] -= q * bid; pos['fills'] += 1   # noqa: E702
                    fills.append(fill_row(t, c, 'BUY', bid, q, 'opt'))
                elif aq > 0 and p > ask:
                    q = min(aq, z); aq -= q; pos['inv'] -= q; pos['cash'] += q * ask; pos['fills'] += 1   # noqa: E702
                    fills.append(fill_row(t, c, 'SELL', ask, q, 'opt'))
                # R4: touching our price fills the realistic ledger too
                if rbq > 0 and p <= bid + eps:
                    q = min(rbq, z); rbq -= q; pos['r_inv'] += q; pos['r_cash'] -= q * bid; pos['r_fills'] += 1   # noqa: E702
                    fills.append(fill_row(t, c, 'BUY', bid, q, 'real'))
                elif raq > 0 and p >= ask - eps:
                    q = min(raq, z); raq -= q; pos['r_inv'] -= q; pos['r_cash'] += q * ask; pos['r_fills'] += 1   # noqa: E702
                    fills.append(fill_row(t, c, 'SELL', ask, q, 'real'))
        pos['mid'] = mid
        pos['share'] = share
        pos['reward'] += earned
        pos['r_reward'] += r_earned
        reward_tick += earned
        r_tick += r_earned
        value = pos['cash'] + pos['inv'] * mid
        mm_value += value
        r_mm += pos['r_cash'] + pos['r_inv'] * mid
        tick['markets'].append({'q': c['question'][:50], 'mid': round(mid, 3), 'rest_q': [round(x, 1) for x in rest],
                                'share': round(share, 4), 'earned': round(earned, 4), 'inv': pos['inv'],
                                'mm_value': round(value, 2)})
    st['reward_total'] += reward_tick
    st['real_reward_total'] = st.get('real_reward_total', 0.0) + r_tick
    bf = st.get('real_backfill') or {}             # the estimated gap before the ledger started
    st.setdefault('real_started', tick['ts'])
    st['last_ts'] = now
    tick.update(reward_tick=round(reward_tick, 4), reward_total=round(st['reward_total'], 2),
                mm_value=round(mm_value, 2), net=round(st['reward_total'] + mm_value, 2),
                avg_share=round(sum(m.get('share', 0) for m in tick['markets']) / max(len(tick['markets']), 1), 4),
                fills_total=sum(p.get('fills', 0) for p in st['positions'].values()),
                real_reward_total=round(st['real_reward_total'], 2), real_mm_value=round(r_mm + bf.get('mm_value', 0.0), 2),
                real_net=round(st['real_reward_total'] + r_mm + bf.get('mm_value', 0.0), 2),
                real_fills_total=sum(p.get('r_fills', 0) for p in st['positions'].values()) + bf.get('fills', 0))
    st['history'].append({k: tick[k] for k in ('ts', 'dt_h', 'reward_tick', 'reward_total', 'mm_value', 'net',
                                               'avg_share', 'fills_total', 'real_reward_total', 'real_mm_value',
                                               'real_net', 'real_fills_total')})
    st['history'] = st['history'][-3000:]
    st['fills'] = fills[-3000:]
    st['last_tick'] = tick
    save(d / 'rewards_state.json', st)
    return tick


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='rewardbot.live')
    ap.add_argument('--state', default='state/rewards')
    a = ap.parse_args(argv)
    t = step(a.state)
    print(json.dumps({k: t[k] for k in ('ts', 'dt_h', 'reward_tick', 'reward_total', 'mm_value', 'net', 'avg_share',
                                        'real_reward_total', 'real_net')}))
    for m in t['markets'][:30]:
        print(' ', m)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
