"""Football value-signal backtest (trial 61).

Pre-registered BEFORE any result was seen (+1 trial -> 61):
  data      football-data.co.uk, 22 main European leagues 2018/19..2025/26 and 16
            extra leagues worldwide from July 2018 (free)
  model     Dixon-Coles per league (footbot.model), refitted every Monday on the
            league's matches of the previous 730 days strictly before that Monday;
            a match is predicted only if both teams have >= 8 matches in the window
  warm-up   no bets before 2019-07-01
  markets   1X2 and over/under 2.5 goals (the only ones with free historical odds)
  price     main leagues: Pinnacle odds as compiled before the round (PSH.., P>2.5..);
            extra leagues: Pinnacle closing odds (nothing earlier exists there)
  signal    edge = p_model * odds - 1 >= 0.05 and odds <= 5.0; at most one bet per
            match (largest edge)
  stake     quarter Kelly, at most 2 % of bankroll per bet; daily return = sum of
            stake * profit per unit over that day's bets
  gates     (1) deflated Sharpe > 0.95 on daily returns with 61 trials
            (2) walk-forward by season (Jul..Jun): >= 60 % of seasons with positive
                return and mean season return > 0
            (3) mean closing-line value > 0 on main-league bets
                (CLV = odds taken * fair Pinnacle closing probability - 1)
  diagnostics (no bets, no gates): log loss of model vs Pinnacle closing for 1X2
            and O/U 2.5; both-teams-to-score and corners over 9.5 vs the training
            window's base rate (no free historical odds for those markets)
"""
from __future__ import annotations

import json
import math
from datetime import timedelta

import numpy as np
import pandas as pd

from footbot import model
from tradebot.engine import Config, metrics
from tradebot.stats import deflated_sharpe

N_TRIALS = 61
WINDOW_DAYS = 730
MIN_TEAM_MATCHES = 8
START = pd.Timestamp('2019-07-01')
MIN_EDGE = 0.05
MAX_ODDS = 5.0
KELLY = 0.25
MAX_STAKE = 0.02
CORNER_LINE = 9.5
SIDES = ['H', 'D', 'A', 'O2.5', 'U2.5']


def fair(odds: list[float]) -> list[float] | None:
    """Proportional de-vig; None if any price is missing."""
    if any(o is None or not o > 1 for o in odds):
        return None
    inv = [1 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv]


def pick(probs: dict, row) -> dict | None:
    best = None
    for s in SIDES:
        o = row.get(f'odds_{s}')
        if o is None or not (o > 1) or o > MAX_ODDS:
            continue
        edge = probs[s] * o - 1
        if edge >= MIN_EDGE and (best is None or edge > best['edge']):
            best = {'side': s, 'odds': float(o), 'prob': probs[s], 'edge': edge}
    if best:
        best['stake'] = min(KELLY * best['edge'] / (best['odds'] - 1), MAX_STAKE)
    return best


def won(side: str, hg: int, ag: int) -> bool:
    return {'H': hg > ag, 'D': hg == ag, 'A': hg < ag,
            'O2.5': hg + ag > 2.5, 'U2.5': hg + ag < 2.5}[side]


def clv(side: str, odds: float, row) -> float | None:
    if side in 'HDA':
        f = fair([row.get('close_H'), row.get('close_D'), row.get('close_A')])
        p = f and f['HDA'.index(side)]
    else:
        f = fair([row.get('close_O2.5'), row.get('close_U2.5')])
        p = f and f[0 if side == 'O2.5' else 1]
    return odds * p - 1 if p else None


def _ll(p: float, y: bool) -> float:
    return -math.log(max(p if y else 1 - p, 1e-6))


def run(data: pd.DataFrame, log=print) -> dict:
    bets, diag = [], {k: [0.0, 0.0, 0] for k in ('1x2', 'ou25', 'btts', 'corners')}
    for league, lg in data.groupby('league'):
        lg = lg.sort_values('date')
        mondays = pd.date_range(START - pd.Timedelta(days=START.weekday()), lg['date'].max(), freq='W-MON')
        for monday in mondays:
            week = lg[(lg['date'] >= monday) & (lg['date'] < monday + timedelta(days=7))]
            if week.empty:
                continue
            train = lg[(lg['date'] < monday) & (lg['date'] >= monday - timedelta(days=WINDOW_DAYS))]
            counts = pd.concat([train['home'], train['away']]).value_counts()
            ok = week[week['home'].map(counts).ge(MIN_TEAM_MATCHES) & week['away'].map(counts).ge(MIN_TEAM_MATCHES)]
            if ok.empty:
                continue
            age = (monday - train['date']).dt.days
            g = model.fit(list(train['home']), list(train['away']), train['hg'], train['ag'], age)
            ctrain = train.dropna(subset=['hc', 'ac'])
            c = None
            if len(ctrain) > 100 and ok['hc'].notna().any():
                c = model.fit(list(ctrain['home']), list(ctrain['away']), ctrain['hc'], ctrain['ac'],
                              (monday - ctrain['date']).dt.days, dixon_coles=False)
            btts_base = float(((train['hg'] > 0) & (train['ag'] > 0)).mean())
            corner_base = float((ctrain['hc'] + ctrain['ac'] > CORNER_LINE).mean()) if len(ctrain) else None
            for _, r in ok.iterrows():
                row = r.to_dict()
                lam, mu = g.rates(row['home'], row['away'])
                probs = model.markets(model.score_matrix(lam, mu, g.rho))
                hg, ag = int(row['hg']), int(row['ag'])
                # diagnostics vs the market and vs base rates
                f = fair([row.get('close_H'), row.get('close_D'), row.get('close_A')])
                if f:
                    res = 'HDA'.index('H' if hg > ag else 'D' if hg == ag else 'A')
                    d = diag['1x2']
                    d[0] += -math.log(max([probs['H'], probs['D'], probs['A']][res], 1e-6))
                    d[1] += -math.log(max(f[res], 1e-6))
                    d[2] += 1
                f = fair([row.get('close_O2.5'), row.get('close_U2.5')])
                if f:
                    d = diag['ou25']
                    d[0] += _ll(probs['O2.5'], hg + ag > 2.5)
                    d[1] += _ll(f[0], hg + ag > 2.5)
                    d[2] += 1
                d = diag['btts']
                d[0] += _ll(probs['BTTS_Y'], hg > 0 and ag > 0)
                d[1] += _ll(btts_base, hg > 0 and ag > 0)
                d[2] += 1
                if c and not math.isnan(row['hc']) and row['home'] in c.teams and row['away'] in c.teams:
                    cl, cm = c.rates(row['home'], row['away'])
                    y = row['hc'] + row['ac'] > CORNER_LINE
                    d = diag['corners']
                    d[0] += _ll(model.total_over(cl, cm, CORNER_LINE), y)
                    d[1] += _ll(corner_base, y)
                    d[2] += 1
                # the bet
                if league in _extra():
                    row = {**row, 'odds_H': row['close_H'], 'odds_D': row['close_D'], 'odds_A': row['close_A']}
                b = pick(probs, row)
                if not b:
                    continue
                w = won(b['side'], hg, ag)
                b.update(date=str(row['date'].date()), league=league, home=row['home'], away=row['away'],
                         won=w, pnl=b['odds'] - 1 if w else -1.0,
                         clv=None if league in _extra() else clv(b['side'], b['odds'], row))
                bets.append(b)
        log(f'{league}: {sum(1 for b in bets if b["league"] == league)} bets')
    return evaluate(bets, diag)


def _extra():
    from footbot.data import EXTRA
    return set(EXTRA)


def season_of(d: pd.Timestamp) -> str:
    y = d.year if d.month >= 7 else d.year - 1
    return f'{y}/{str(y + 1)[2:]}'


def evaluate(bets: list[dict], diag: dict) -> dict:
    out = {'n_trials': N_TRIALS, 'n_bets': len(bets),
           'diagnostics': {k: {'model': round(v[0] / v[2], 4), 'reference': round(v[1] / v[2], 4), 'n': v[2]}
                           for k, v in diag.items() if v[2]}}
    if len(bets) < 100:
        out['verdict'] = 'NOT ENOUGH DATA'
        return out
    t = pd.DataFrame(bets)
    t['date'] = pd.to_datetime(t['date'])
    t['ret'] = t['stake'] * t['pnl']
    out.update({'hit_rate': round(float(t['won'].mean()), 3), 'avg_odds': round(float(t['odds'].mean()), 2),
                'avg_edge': round(float(t['edge'].mean()), 3),
                'roi_flat': round(float(t['pnl'].mean()) * 100, 2),
                'total_return': round(float(t['ret'].sum()) * 100, 2),
                'by_side': t.groupby('side')['pnl'].agg(['count', 'mean']).round(4).to_dict('index'),
                'by_league': t.groupby('league')['pnl'].agg(['count', 'mean']).round(4).to_dict('index')})
    c = t['clv'].dropna()
    out['clv'] = {'n': len(c), 'mean': round(float(c.mean()), 4) if len(c) else None,
                  'positive_share': round(float((c > 0).mean()), 3) if len(c) else None}
    daily = t.groupby('date')['ret'].sum().sort_index()
    per_year = len(daily) / max((daily.index[-1] - daily.index[0]).days / 365.25, 1e-9)
    m = metrics(daily, Config(periods_per_year=int(round(per_year))), min_obs=30)
    out['metrics'] = m
    dsr = deflated_sharpe(m['sharpe'], N_TRIALS, m['n_obs'], m['skew'], m['kurtosis'], int(round(per_year)))
    out['dsr'] = dsr
    seasons = t.groupby(t['date'].map(season_of))['ret'].sum()
    out['seasons'] = {k: round(float(v) * 100, 2) for k, v in seasons.items()}
    gates = {'deflated_sharpe': dsr['deflated_sharpe'] > 0.95,
             'walk_forward': (seasons > 0).mean() >= 0.6 and seasons.mean() > 0,
             'clv': bool(len(c)) and c.mean() > 0}
    out['gates'] = gates
    out['verdict'] = 'APPROVED' if all(gates.values()) else 'REJECTED'
    return out


def main(argv=None) -> int:
    import argparse
    from footbot import data
    ap = argparse.ArgumentParser(prog='footbot.backtest')
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    res = run(data.load())
    print(json.dumps({k: v for k, v in res.items() if k != 'by_league'}, indent=2, default=str))
    print(json.dumps(res.get('by_league', {}), default=str))
    if a.out:
        with open(a.out, 'w') as f:
            json.dump(res, f, indent=1, default=str)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
