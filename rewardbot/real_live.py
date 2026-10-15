"""Real money with rewards v4, phase 2: LIVE orders on Polymarket. Off unless the owner turns it on.

Runs only when BOTH are set: the secret POLY_PRIVATE_KEY (with POLY_FUNDER, the Polymarket wallet
address) and the repository variable REAL_TRADING = on. Otherwise the dry run (rewardbot.real) goes on.
The key is read from the environment, handed to Polymarket's own client and never logged or saved.

Every minute
  1. owner commands: an open issue by the repository owner titled 'riktiga pengar: stopp' halts
     trading (all orders cancelled), 'riktiga pengar: start' resumes it; the bot answers and closes it
  2. account: USDC balance, positions and profit/loss from Polymarket's public positions API
  3. loss limit: if the trading result (Polymarket's per-position profit/loss, rewards NOT counted)
     has fallen REAL_LOSS_LIMIT USD (default 10) since the start, everything is cancelled and trading
     halts until the owner starts it again
  4. orders: the dry-run planner's orders for REAL_CAPITAL USD (default 50), minus bids for tokens
     already held (inventory cap 1 x min size); open orders that are no longer wanted are cancelled,
     missing ones are posted post-only (they can never trade on arrival, only rest in the book)
  5. scoring: Polymarket is asked which of our orders earn rewards right now - the first hard
     check of the model
The loop's last step, and a separate always() step in the workflow, cancel every order, so nothing
rests unwatched between runs.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from rewardbot import real

FILE = 'real_live.json'
HOST = 'https://clob.polymarket.com'
DATA_API = 'https://data-api.polymarket.com'
STOP, START = 'riktiga pengar: stopp', 'riktiga pengar: start'
MAX_POSTS_PER_TICK = 12


def env(name: str, default: str = '') -> str:
    return (os.environ.get(name) or default).strip()


def enabled() -> bool:
    return env('REAL_TRADING').lower() == 'on' and bool(env('POLY_PRIVATE_KEY')) and bool(env('POLY_FUNDER'))


def _num(name: str, default: float) -> float:
    try:
        return float(env(name) or default)
    except ValueError:                                  # a mistyped variable falls back to the safe default
        return default


def capital() -> float:
    return _num('REAL_CAPITAL', 50.0)


def loss_limit() -> float:
    return _num('REAL_LOSS_LIMIT', 10.0)


def client():
    """Polymarket's own client, with API credentials derived from the key (L2 auth)."""
    from py_clob_client.client import ClobClient
    c = ClobClient(HOST, chain_id=137, key=env('POLY_PRIVATE_KEY'), signature_type=int(env('POLY_SIGNATURE_TYPE', '1') or 1),
                   funder=env('POLY_FUNDER'))
    c.set_api_creds(c.create_or_derive_api_creds())
    return c


# ---- owner commands through GitHub issues (anyone can open an issue on a public repo: only the owner counts)
def commands(http) -> list[dict]:
    repo, token = env('GITHUB_REPOSITORY'), env('GITHUB_TOKEN')
    if not repo or not token:
        return []
    try:
        r = http.get(f'https://api.github.com/repos/{repo}/issues', params={'state': 'open', 'per_page': 50},
                     headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}, timeout=20)
        issues = r.json() if r.status_code == 200 else []
    except Exception:                                   # noqa: BLE001
        return []
    owner = repo.split('/')[0]
    out = []
    for i in issues:
        t = (i.get('title') or '').strip().lower() if isinstance(i, dict) else ''
        if 'pull_request' in i or (i.get('user') or {}).get('login') != owner or t not in (STOP, START):
            continue
        out.append({'number': i['number'], 'cmd': 'stop' if t == STOP else 'start'})
    return out


def answer(http, number: int, text: str) -> None:
    repo, token = env('GITHUB_REPOSITORY'), env('GITHUB_TOKEN')
    h = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
    try:
        http.post(f'https://api.github.com/repos/{repo}/issues/{number}/comments', json={'body': text}, headers=h, timeout=20)
        http.patch(f'https://api.github.com/repos/{repo}/issues/{number}', json={'state': 'closed'}, headers=h, timeout=20)
    except Exception:                                   # noqa: BLE001
        pass


# ---- account
def account(http, cli) -> dict:
    """USDC balance (free cash), positions and their profit/loss as Polymarket reports them."""
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    bal = cli.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                                                           signature_type=int(env('POLY_SIGNATURE_TYPE', '1') or 1)))
    cash = float((bal or {}).get('balance') or 0) / 1e6
    try:
        r = http.get(f'{DATA_API}/positions', params={'user': env('POLY_FUNDER'), 'sizeThreshold': 0}, timeout=20)
        pos = r.json() if r.status_code == 200 else []
    except Exception:                                   # noqa: BLE001
        pos = []
    held = {str(p.get('asset')): float(p.get('size') or 0) for p in pos or [] if float(p.get('size') or 0) > 0}
    value = sum(float(p.get('currentValue') or 0) for p in pos or [])
    # trading result as Polymarket reports it per position (unrealised + realised); rewards are not in it,
    # so the loss limit only ever errs on the careful side
    trade_pnl = sum(float(p.get('cashPnl') or 0) + float(p.get('realizedPnl') or 0) for p in pos or [])
    return {'cash': round(cash, 4), 'positions_value': round(value, 4), 'held': held, 'trade_pnl': round(trade_pnl, 4),
            'positions': [{'q': (p.get('title') or '')[:60], 'outcome': p.get('outcome'), 'size': p.get('size'),
                           'avg': p.get('avgPrice'), 'now': p.get('curPrice'), 'pnl': p.get('cashPnl')} for p in pos or []]}


def _open_orders(cli) -> list[dict]:
    return [o for o in cli.get_orders() or [] if str(o.get('status', 'LIVE')).upper() in ('LIVE', 'OPEN', '')]


def cancel_everything(cli, log=print) -> None:
    try:
        cli.cancel_all()
        log('real: cancelled all orders')
    except Exception as e:                              # noqa: BLE001
        log(f'real: cancel_all failed: {type(e).__name__}')


def tick(state: str, http, now: float | None = None, log=print, cli=None) -> dict:
    now = now or time.time()
    d = Path(state)
    path = d / FILE
    st = json.loads(path.read_text()) if path.exists() else {'halted': False, 'events': [], 'history': []}
    iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
    ev = lambda kind, **kw: st['events'].append({'ts': iso, 'kind': kind, **kw})   # noqa: E731
    cli = cli or client()
    # 1. owner commands
    for c in commands(http):
        if c['cmd'] == 'stop':
            st.update(halted=True, halt_reason='stoppad av dig')
            cancel_everything(cli, log)
            ev('stopp', why='ägaren')
            answer(http, c['number'], 'Stoppat: alla ordrar är borttagna och boten lägger inga nya förrän du skapar "riktiga pengar: start".')
        else:
            st.update(halted=False, halt_reason=None, base=None)     # a restart measures the loss limit from now
            ev('start', why='ägaren')
            answer(http, c['number'], 'Startat igen. Förlustgränsen räknas från nu.')
    # 2. account and the loss limit
    acc = account(http, cli)
    open_now = _open_orders(cli)
    equity = acc['cash'] + acc['positions_value']        # display only: whether open orders reduce 'cash' is not relied on
    if st.get('base') is None:
        st['base'], st['base_ts'] = acc['trade_pnl'], iso
    pnl = acc['trade_pnl'] - st['base']
    if not st['halted'] and pnl <= -loss_limit():
        st.update(halted=True, halt_reason=f'förlustgränsen {loss_limit():.0f} $ nådd')
        ev('stopp', why='förlustgräns', pnl=round(pnl, 2))
        log(f'real: loss limit hit ({pnl:.2f} USD), halting')
    # 3. orders
    posted = cancelled = 0
    plan = {'markets': [], 'orders': [], 'est_reward_day': 0.0, 'used': 0.0}
    if st['halted']:
        if open_now:
            cancel_everything(cli, log)
            cancelled = len(open_now)
        open_now = []
    else:
        ticks = st.setdefault('ticks', {})
        plan = real.compute(state, http, capital(), ticks, acc['held'])   # Polymarket itself refuses orders beyond the balance
        key = lambda tok, price, size: (str(tok), round(float(price), 4), round(float(size), 2))   # noqa: E731
        want = {key(o['token'], o['price'], o['size']): o for o in plan['orders']}
        have = {key(o.get('asset_id'), o.get('price'), float(o.get('original_size') or 0)): o for o in open_now}
        stale = [o['id'] for k, o in have.items() if k not in want]
        if stale:
            try:
                cli.cancel_orders(stale)
                cancelled = len(stale)
            except Exception as e:                      # noqa: BLE001
                ev('fel', msg=f'cancel: {type(e).__name__}')
                cancel_everything(cli, log)             # when in doubt, nothing stays out
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        for k, o in want.items():
            if k in have or posted >= MAX_POSTS_PER_TICK:
                continue
            try:
                signed = cli.create_order(OrderArgs(token_id=o['token'], price=o['price'], size=o['size'], side=BUY))
                r = cli.post_order(signed, OrderType.GTC, post_only=True)
                ok = bool((r or {}).get('success', True)) and not (r or {}).get('errorMsg')
                posted += ok
                if not ok:
                    ev('avvisad', q=o['cond'][:10], msg=str((r or {}).get('errorMsg'))[:120])
            except Exception as e:                      # noqa: BLE001
                ev('fel', q=o['cond'][:10], msg=f'{type(e).__name__}: {str(e)[:120]}')
        open_now = _open_orders(cli) if (posted or cancelled) else open_now
    # 4. which orders earn rewards right now (Polymarket's own answer)
    scoring = {}
    if open_now:
        try:
            from py_clob_client.clob_types import OrdersScoringParams
            scoring = cli.are_orders_scoring(OrdersScoringParams(orderIds=[o['id'] for o in open_now])) or {}
        except Exception as e:                          # noqa: BLE001
            log(f'real: scoring check failed: {type(e).__name__}')
    n_scoring = sum(1 for o in open_now if scoring.get(o['id']))
    row = {'ts': iso, 'equity': round(equity, 4), 'pnl': round(pnl, 4), 'cash': acc['cash'], 'positions_value': acc['positions_value'],
           'orders': len(open_now), 'scoring': n_scoring, 'est_reward_day': plan['est_reward_day'], 'posted': posted, 'cancelled': cancelled}
    hist = st['history']
    if hist and hist[-1]['ts'][:15] == iso[:15]:      # one row per 10 minutes
        hist[-1] = row
    else:
        hist.append(row)
    st.update(mode='halted' if st['halted'] else 'live', ts=iso, capital=capital(), loss_limit=loss_limit(), equity=round(equity, 4),
              pnl=round(pnl, 4), account={k: acc[k] for k in ('cash', 'positions_value', 'positions')},
              orders=[{'id': o['id'][:12], 'token': str(o.get('asset_id')), 'price': o.get('price'), 'size': o.get('original_size'),
                       'filled': o.get('size_matched'), 'scoring': bool(scoring.get(o['id']))} for o in open_now],
              markets=plan['markets'], est_reward_day=plan['est_reward_day'])
    st['history'], st['events'] = hist[-5000:], st['events'][-2000:]
    path.write_text(json.dumps(st, separators=(',', ':'), default=str))
    log(f"real LIVE {iso}: {len(open_now)} orders ({n_scoring} scoring), +{posted}/-{cancelled}, equity {equity:.2f}, pnl {pnl:+.2f}"
        + (f" HALTED: {st.get('halt_reason')}" if st['halted'] else ''))
    return row


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='rewardbot.real_live')
    ap.add_argument('--cancel-all', action='store_true', help='cancel every open order (safe to run any time)')
    a = ap.parse_args(argv)
    if a.cancel_all:
        if not (env('POLY_PRIVATE_KEY') and env('POLY_FUNDER')):
            print('real: no key configured, nothing to cancel')
            return 0
        cancel_everything(client())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
