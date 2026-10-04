"""Paper trading on live Polymarket temperature markets.

Runs hourly. Each run:
  1. settles positions whose event has resolved
  2. logs the blended forecast of every open event once its trading window
     opens (06:00-08:00 local on the event day -- the backtest's 06:00 rule),
     so the calibration keeps learning from every resolved event
  3. buys at the real best ask (YES or NO token), never more than the size
     offered there, with the same edge rule and sizing as the backtest

State (JSON in the state directory):
  weather_state.json   cash, open/closed positions, forecast log, equity history
  calibration.json     residual history (from the backtest, then live)
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from weatherbot import forecast, markets, model

START_CASH = 1000.0
WINDOW = (6, 8)  # local hours in which a city-day may be traded


def load(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=1, default=str))


def calibration_from(rows: list) -> model.Calibration:
    c = model.Calibration()
    c.rows = [(date.fromisoformat(d), float(r)) for d, r in rows]
    return c


def fetch_event(event_id: str, http) -> markets.Event | None:
    r = http.get(f'{markets.GAMMA}/events/{event_id}', timeout=30)
    r.raise_for_status()
    return markets.parse_event(r.json())


def _mid(yes_ask, no_ask):
    """Market-implied YES probability: midpoint of the YES ask and 1 - NO ask."""
    if yes_ask is None or no_ask is None:
        return None
    return round((yes_ask + 1 - no_ask) / 2, 4)


def score(st: dict, logged: dict, ev: markets.Event) -> None:
    """Running log-loss of the model and of the market on resolved buckets."""
    if 'probs' not in logged:
        return
    sc = st.setdefault('score', {'n': 0, 'model': 0.0, 'market': 0.0, 'events': 0})
    won = {b.label: bool(b.resolved_yes) for b in ev.buckets}
    for label, p, q in zip(logged['labels'], logged['probs'], logged['market']):
        if q is None or label not in won:
            continue
        y = won[label]
        sc['model'] += -math.log(max(p if y else 1 - p, 1e-4))
        sc['market'] += -math.log(max(q if y else 1 - q, 1e-4))
        sc['n'] += 1
    sc['events'] += 1


def equity(state: dict) -> float:
    return state['cash'] + sum(p['stake_usd'] for p in state['open'])


def step(state_dir: str | Path, now: datetime | None = None, http=None, approved: bool = True,
         log=print, gates_ignored: bool = False) -> dict:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    http = http or requests.Session()
    now = now or datetime.now(timezone.utc)
    st = load(d / 'weather_state.json', {'cash': START_CASH, 'open': [], 'closed': [],
                                         'forecast_log': {}, 'history': []})
    cal_rows = load(d / 'calibration.json', {'rows': []})['rows']
    report = {'ts': now.isoformat(), 'settled': [], 'opened': [], 'logged': 0, 'skipped': {}}

    # 1. settle resolved positions and log residuals for every logged event
    for eid in list({p['event_id'] for p in st['open']} | set(st['forecast_log'])):
        try:
            ev = fetch_event(eid, http)
        except requests.RequestException as exc:
            log(f'event {eid}: {exc}')
            continue
        if not ev or not ev.winner:
            continue
        logged = st['forecast_log'].pop(eid, None)
        if logged:
            cal_rows.append([logged['date'], (ev.winner.center() - logged['mu_raw'])
                             * (model.C_PER_F if ev.unit == 'F' else 1.0)])
            score(st, logged, ev)
        for p in [p for p in st['open'] if p['event_id'] == eid]:
            b = next(b for b in ev.buckets if b.label == p['bucket'])
            won = bool(b.resolved_yes) if p['side'] == 'YES' else not b.resolved_yes
            payout = p['shares'] if won else 0.0
            st['cash'] += payout
            p.update(won=won, pnl_usd=round(payout - p['stake_usd'], 4), settled=now.isoformat())
            st['open'].remove(p)
            st['closed'].append(p)
            report['settled'].append(p)

    # 2./3. open events in their trading window
    try:
        events = markets.fetch_events(False, pages=3, session=http)
    except requests.RequestException as exc:
        log(f'event list failed: {exc}')
        events = []
    calib = calibration_from(cal_rows)
    traded = {p['event_id'] for p in st['open'] + st['closed']}
    bankroll = equity(st)
    for ev in events:
        coords = forecast.station_coords(ev.station)
        if not coords or ev.id in traded or ev.id in st['forecast_log']:
            continue
        try:
            daily, offset = forecast.live_forecast(*coords, ev.unit, http)
        except requests.RequestException as exc:
            report['skipped'][ev.city] = f'forecast: {exc}'
            continue
        local = now + timedelta(seconds=offset)
        if local.date() != ev.date or not WINDOW[0] <= local.hour < WINDOW[1]:
            continue
        blended = forecast.blend(daily.get(ev.date, {}))
        if not blended:
            report['skipped'][ev.city] = 'too few models'
            continue
        mu_raw, spread, n_models = blended
        st['forecast_log'][ev.id] = {'date': str(ev.date), 'mu_raw': mu_raw, 'unit': ev.unit,
                                     'city': ev.city, 'spread': spread}
        report['logged'] += 1
        cal = calib.params(before=ev.date, unit=ev.unit)
        if cal is None:
            report['skipped'][ev.city] = 'calibration not ready'
            continue
        bias, sigma = cal
        probs = [model.bucket_prob(mu_raw + bias, sigma, b.lo, b.hi) for b in ev.buckets]
        yes_asks, no_asks, sizes = [], [], {}
        for b in ev.buckets:
            try:
                _, ya, ys = markets.best_quotes(b.yes_token, http)
                _, na, ns = markets.best_quotes(b.no_token, http)
            except requests.RequestException:
                ya = na = None
                ys = ns = 0.0
            yes_asks.append(ya)
            no_asks.append(na)
            sizes[(b.label, 'YES')], sizes[(b.label, 'NO')] = ys, ns
        # keep what the model and the market said, to score both once it resolves
        st['forecast_log'][ev.id].update(
            labels=[b.label for b in ev.buckets], probs=[round(p, 4) for p in probs],
            market=[_mid(ya, na) for ya, na in zip(yes_asks, no_asks)])
        if not approved:
            report['skipped'][ev.city] = 'strategy not approved (forecast and prices logged)'
            continue
        for bet in model.select_bets_quotes(ev.buckets, probs, yes_asks, no_asks):
            # whole cents, rounded down so the per-event cap is never exceeded
            stake = math.floor(min(bet.stake * bankroll, sizes[(bet.bucket, bet.side)] * bet.cost) * 100) / 100
            b = next(x for x in ev.buckets if x.label == bet.bucket)
            # Polymarket rejects orders below orderMinSize shares
            if stake < 1.0 or stake > st['cash'] or stake / bet.cost < b.min_size:
                continue
            pos = {'event_id': ev.id, 'slug': ev.slug, 'city': ev.city, 'date': str(ev.date),
                   'station': ev.station, 'bucket': bet.bucket, 'side': bet.side,
                   'token': b.yes_token if bet.side == 'YES' else b.no_token,
                   'price': bet.price, 'cost': round(bet.cost, 4), 'prob': round(bet.prob, 4),
                   'edge': round(bet.edge, 4), 'shares': round(stake / bet.cost, 4),
                   'stake_usd': stake, 'mu': round(mu_raw + bias, 2),
                   'sigma': round(sigma, 2), 'opened': now.isoformat()}
            st['cash'] -= stake
            st['open'].append(pos)
            report['opened'].append(pos)

    st['mode'] = 'trading' if approved else 'measuring'
    st['gates_ignored'] = gates_ignored
    st['history'].append({'ts': now.isoformat(), 'equity': round(equity(st), 2),
                          'cash': round(st['cash'], 2), 'open': len(st['open'])})
    st['history'] = st['history'][-2000:]
    st['last_run'] = report
    save(d / 'weather_state.json', st)
    save(d / 'calibration.json', {'rows': cal_rows[-5000:]})
    return report
