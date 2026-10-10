"""Back-test of version 3's portfolio (trial 64).

The share of the reward pool cannot be checked historically: Polymarket keeps no
old order books, only the one live now. What CAN be replayed is everything else:
the real trades of the last 14 days (fills, adverse selection, inventory) and the
hours in which the paid conditions held. The reward is therefore reported as a
range over an assumed share.

Pre-registered BEFORE any result was seen (+1 trial -> 64):
  markets    version 3's portfolio as selected (rewards/rewards_v3_state.json), each
             with its rate_per_day, v, min size and the share measured at selection
  window     the last 14 days, hourly; a market counts from its first trade
  quotes     min size at (mean of the last 10 trades) +/- v/2, re-placed every hour,
             using only earlier trades (no look-ahead)
  fills      a trade AT or through our price fills us (version 3's realistic rule R4);
             inventory at most 1 x min size either way; marked to the last trade
  reward     per hour rate/24 * share, only while the reference is in [0.10, 0.90]
             and moved at most v - d cents since the previous hour (R2, R3)
  shares     A: the share measured at selection (an upper bound: we were alone)
             B: one tenth of it (competition arrives)
             also reported: the break-even multiple k* of the measured share at which
             reward + market making = 0
  gates      on variant B (the cautious one): deflated Sharpe > 0.95 with 64 trials
             on daily returns of 1000 USD; 2-day folds >= 60 % positive with mean > 0
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

from rewardbot import backtest as bt
from tradebot.engine import Config, metrics
from tradebot.stats import deflated_sharpe

N_TRIALS = 64
DAYS = 14
CAPITAL = 1000.0
FOLD_DAYS = 2
SHARE_B = 0.1


def replay(trades: list, start: int, end: int, c: dict) -> tuple[dict, dict]:
    """Hourly replay of one market. Returns ({day: reward at full measured share}, {day: market-making P&L})."""
    d, size, v = c['d'], c['min_size'], c['v']
    cap = size
    inv = cash = 0.0
    j, recent, ref, prev_ref, last_px = 0, [], None, None, None
    rew, mm, day_val, cur = {}, {}, 0.0, None
    for t in range(start - start % 3600, end, 3600):
        while j < len(trades) and trades[j][0] < t:
            last_px = trades[j][1]
            recent = (recent + [last_px])[-10:]
            ref = sum(recent) / len(recent)
            j += 1
        day = datetime.fromtimestamp(t, timezone.utc).date()
        if day != cur:
            if cur is not None and last_px is not None:
                mm[cur] = cash + inv * last_px - day_val
            cur = day
            day_val = cash + inv * (last_px if last_px is not None else 0.0)
        if ref is None:
            continue
        ok = 0.10 <= ref <= 0.90 and (prev_ref is None or abs(ref - prev_ref) * 100 <= v - d * 100)
        prev_ref = ref
        if ok:
            rew[day] = rew.get(day, 0.0) + c['rate'] * c['share'] / 24
        bid, ask = ref - d, ref + d
        bq = size if inv + size <= cap else 0.0
        aq = size if -inv + size <= cap else 0.0
        k = j
        while k < len(trades) and trades[k][0] < t + 3600:
            p, z = trades[k][1], trades[k][2]
            if bq > 0 and p <= bid + 1e-9:
                q = min(bq, z); bq -= q; inv += q; cash -= q * bid          # noqa: E702
            elif aq > 0 and p >= ask - 1e-9:
                q = min(aq, z); aq -= q; inv -= q; cash += q * ask          # noqa: E702
            k += 1
    if cur is not None and last_px is not None:
        mm[cur] = cash + inv * last_px - day_val
    return rew, mm


def evaluate(daily: pd.Series) -> dict:
    daily = daily.sort_index()
    out = {'days': len(daily), 'net': round(float(daily.sum()), 2)}
    if len(daily) < 2 * FOLD_DAYS:
        out['verdict'] = 'NOT ENOUGH DATA'
        return out
    daily.index = pd.to_datetime(daily.index)
    rets = daily / CAPITAL
    m = metrics(rets, Config(periods_per_year=365), min_obs=FOLD_DAYS)
    folds = [float(rets.iloc[i:i + FOLD_DAYS].sum()) for i in range(0, len(rets) - FOLD_DAYS + 1, FOLD_DAYS)]
    dsr = deflated_sharpe(m['sharpe'], N_TRIALS, m['n_obs'], m['skew'], m['kurtosis'], 365)
    gates = {'deflated_sharpe': bool(dsr['deflated_sharpe'] > 0.95),
             'walk_forward': bool(sum(f > 0 for f in folds) / len(folds) >= 0.6 and np.mean(folds) > 0)}
    out.update(metrics=m, dsr=dsr, folds={'n': len(folds), 'positive': f'{sum(f > 0 for f in folds)}/{len(folds)}',
                                          'pnl_usd': [round(f * CAPITAL, 2) for f in folds]},
               gates=gates, verdict='APPROVED' if all(gates.values()) else 'REJECTED')
    return out


def run(portfolio: list[dict], trades_of, now: int) -> dict:
    start0 = now - DAYS * 86400
    R, M, detail = pd.Series(dtype=float), pd.Series(dtype=float), []
    for c in portfolio:
        tr = trades_of(c)
        if not tr:
            detail.append({'q': c['question'][:60], 'trades': 0})
            continue
        rew, mm = replay(tr, max(start0, tr[0][0]), now, c)
        r, m = pd.Series(rew, dtype=float), pd.Series(mm, dtype=float)
        R, M = R.add(r, fill_value=0.0), M.add(m, fill_value=0.0)
        detail.append({'q': c['question'][:60], 'trades': len(tr), 'share': round(c['share'], 3), 'rate': c['rate'],
                       'reward_A': round(float(r.sum()), 2), 'mm': round(float(m.sum()), 2)})
    days = sorted(set(R.index) | set(M.index))
    R, M = R.reindex(days).fillna(0.0), M.reindex(days).fillna(0.0)
    mm_total, rew_total = float(M.sum()), float(R.sum())
    out = {'n_trials': N_TRIALS, 'markets': len(portfolio), 'with_trades': sum(1 for x in detail if x.get('trades')),
           'days': len(days), 'mm_total': round(mm_total, 2),
           'A_full_share': {'reward': round(rew_total, 2), **evaluate(R + M)},
           'B_tenth_share': {'reward': round(rew_total * SHARE_B, 2), **evaluate(R * SHARE_B + M)},
           'break_even_multiple': round(-mm_total / rew_total, 4) if rew_total > 0 else None,
           'per_day': {str(k): {'reward_A': round(float(R[k]), 2), 'mm': round(float(M[k]), 2)} for k in days},
           'markets_detail': sorted(detail, key=lambda x: -(x.get('reward_A') or 0))}
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='rewardbot.v3_backtest')
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    st = json.loads(subprocess.run(['git', 'show', 'origin/tradebot-state:rewards/rewards_v3_state.json'],
                                   capture_output=True, text=True, check=True).stdout)
    port = [{**c, 'share': (st['positions'].get(c['cond']) or {}).get('share') or 0.0} for c in st['portfolio']]
    http, now = requests.Session(), int(time.time())
    res = run(port, lambda c: bt.fetch_trades(c['cond'], c['yes'], now - DAYS * 86400, http), now)
    print(json.dumps({k: v for k, v in res.items() if k not in ('per_day', 'markets_detail')}, indent=2, default=str))
    print(json.dumps(res['per_day'], indent=1))
    for x in res['markets_detail']:
        print(' ', x)
    if a.out:
        with open(a.out, 'w') as f:
            json.dump(res, f, indent=1, default=str)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
