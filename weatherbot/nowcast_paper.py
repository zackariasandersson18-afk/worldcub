"""Paper trading of the late-day nowcast (weatherbot.nowcast, 16:00 solar variant).

The backtest approved this variant, but only against historical mid/last
prices. This forward test answers what the backtest cannot: are those prices
really payable at the ask, late in the day? Virtual money only.

Each run (twice an hour):
  1. settles resolved positions and scores model vs market on them
  2. once per UTC day rebuilds the remaining-rise distribution from METAR
     (complete solar days before today only, the backtest's rule)
  3. for every open event whose station is between 16:00 and 18:00 solar time
     on the event day: observed high = METAR max strictly before 16:00 solar,
     probabilities as in the backtest, buys at the real best ask with the
     same edge, fee, size and minimum-order rules as weatherbot.paper

State (JSON in the state directory):
  nowcast_state.json   cash, open/closed positions, decision log, score, equity history
  deltas.json          remaining-rise training rows [day, unit, delta]
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from weatherbot import forecast, markets, model, nowcast
from weatherbot.paper import START_CASH, _mid, equity, fetch_event, load, save

HOUR = 16
WINDOW_END = 18          # trade until 18:00 solar if the 16:xx run was missed
HISTORY_DAYS = 45


def solar_now(now: datetime, lon: float) -> datetime:
    return now + timedelta(seconds=nowcast.solar_offset_s(lon))


def rebuild_deltas(stations: dict[str, str], now: datetime, http, log=print) -> list:
    """stations: {icao: unit}. Rows from complete solar days only (before the station's solar today)."""
    today = now.date()
    rows = []
    for st, unit in sorted(stations.items()):
        coords = forecast.station_coords(st)
        if not coords:
            continue
        try:
            obs = nowcast.fetch_metar(st, today - timedelta(days=HISTORY_DAYS), today, http)
        except requests.RequestException as exc:
            log(f'metar {st}: {exc}')
            continue
        solar_today = solar_now(now, coords[1]).date()
        days = nowcast.daily_obs(obs, coords[1], unit, HOUR)
        first = min(days, default=None)  # starts at UTC midnight, so its solar morning is cut off
        for d, (m, full) in days.items():
            if first < d < solar_today:
                rows.append([str(d), unit, full - m])
    return rows


def dist_from(rows: list) -> nowcast.DeltaDist:
    dd = nowcast.DeltaDist()
    for d, unit, delta in rows:
        dd.add(date.fromisoformat(d), unit, int(delta))
    return dd


def score(st: dict, logged: dict, ev: markets.Event) -> None:
    sc = st.setdefault('score', {'n': 0, 'model': 0.0, 'market': 0.0, 'events': 0})
    won = {b.label: bool(b.resolved_yes) for b in ev.buckets}
    ll = lambda p, y: -math.log(max(p if y else 1 - p, 1e-4))  # noqa: E731
    for label, p, q in zip(logged['labels'], logged['probs'], logged['market']):
        if q is None or label not in won:
            continue
        sc['model'] += ll(p, won[label])
        sc['market'] += ll(q, won[label])
        sc['n'] += 1
    sc['events'] += 1


def step(state_dir: str | Path, now: datetime | None = None, http=None, log=print) -> dict:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    http = http or requests.Session()
    now = now or datetime.now(timezone.utc)
    st = load(d / 'nowcast_state.json', {'cash': START_CASH, 'open': [], 'closed': [],
                                         'decisions': {}, 'history': []})
    deltas = load(d / 'deltas.json', {'built': None, 'rows': []})
    report = {'ts': now.isoformat(), 'settled': [], 'opened': [], 'decided': 0, 'skipped': {}}

    # 1. settle and score
    pending = {p['event_id'] for p in st['open']} | {k for k, v in st['decisions'].items() if not v.get('scored')}
    for eid in pending:
        try:
            ev = fetch_event(eid, http)
        except requests.RequestException as exc:
            log(f'event {eid}: {exc}')
            continue
        if not ev or not ev.winner:
            continue
        logged = st['decisions'].get(eid)
        if logged and not logged.get('scored'):
            score(st, logged, ev)
            logged['scored'] = True
            logged['winner'] = ev.winner.label
        for p in [p for p in st['open'] if p['event_id'] == eid]:
            b = next(b for b in ev.buckets if b.label == p['bucket'])
            won = bool(b.resolved_yes) if p['side'] == 'YES' else not b.resolved_yes
            payout = p['shares'] if won else 0.0
            st['cash'] += payout
            p.update(won=won, pnl_usd=round(payout - p['stake_usd'], 4), settled=now.isoformat())
            st['open'].remove(p)
            st['closed'].append(p)
            report['settled'].append(p)

    try:
        events = markets.fetch_events(False, pages=3, session=http)
    except requests.RequestException as exc:
        log(f'event list failed: {exc}')
        events = []
    events = [e for e in events if forecast.station_coords(e.station)]

    # 2. remaining-rise distribution, rebuilt once per UTC day
    today = now.date()
    if deltas['built'] != str(today) and events:
        rows = rebuild_deltas({e.station: e.unit for e in events}, now, http, log)
        if rows:
            deltas = {'built': str(today), 'rows': rows}
    dist = dist_from(deltas['rows'])

    # 3. decide and trade
    bankroll = equity(st)
    upcoming = []
    for ev in events:
        lat, lon = forecast.station_coords(ev.station)
        solar = solar_now(now, lon)
        if solar.date() != ev.date:
            continue
        hours = solar.hour + solar.minute / 60
        upcoming.append({'city': ev.city, 'station': ev.station, 'slug': ev.slug, 'solar': solar.strftime('%H:%M')})
        if ev.id in st['decisions'] or not HOUR <= hours < WINDOW_END:
            continue
        dp = dist.probs(before=ev.date, unit=ev.unit)
        if dp is None:
            report['skipped'][ev.city] = 'too few training days'
            continue
        try:
            obs = nowcast.fetch_metar(ev.station, ev.date - timedelta(days=1), ev.date, http)
        except requests.RequestException as exc:
            report['skipped'][ev.city] = f'metar: {exc}'
            continue
        cutoff = nowcast.solar_day_start(ev.date, lon) + HOUR * 3600
        today_obs = [(t, f) for t, f in obs if nowcast.solar_day_start(ev.date, lon) <= t < cutoff]
        if not today_obs:
            report['skipped'][ev.city] = 'no METAR before 16:00 solar'
            continue
        m = max(nowcast.to_unit(f, ev.unit) for _, f in today_obs)
        probs = nowcast.bucket_probs(m, dp, ev.buckets)
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
        st['decisions'][ev.id] = {
            'city': ev.city, 'station': ev.station, 'date': str(ev.date), 'unit': ev.unit, 'slug': ev.slug,
            'observed': m, 'last_obs': datetime.fromtimestamp(today_obs[-1][0], timezone.utc).isoformat(),
            'solar_time': solar.strftime('%H:%M'), 'decided': now.isoformat(),
            'labels': [b.label for b in ev.buckets], 'probs': [round(p, 4) for p in probs],
            'market': [_mid(ya, na) for ya, na in zip(yes_asks, no_asks)],
            'yes_ask': yes_asks, 'no_ask': no_asks}
        report['decided'] += 1
        for bet in model.select_bets_quotes(ev.buckets, probs, yes_asks, no_asks):
            stake = math.floor(min(bet.stake * bankroll, sizes[(bet.bucket, bet.side)] * bet.cost) * 100) / 100
            b = next(x for x in ev.buckets if x.label == bet.bucket)
            if stake < 1.0 or stake > st['cash'] or stake / bet.cost < b.min_size:
                continue
            pos = {'event_id': ev.id, 'slug': ev.slug, 'city': ev.city, 'date': str(ev.date),
                   'station': ev.station, 'bucket': bet.bucket, 'side': bet.side,
                   'token': b.yes_token if bet.side == 'YES' else b.no_token,
                   'price': bet.price, 'cost': round(bet.cost, 4), 'prob': round(bet.prob, 4),
                   'edge': round(bet.edge, 4), 'shares': round(stake / bet.cost, 4),
                   'stake_usd': stake, 'observed': m, 'opened': now.isoformat()}
            st['cash'] -= stake
            st['open'].append(pos)
            report['opened'].append(pos)

    # keep the decision log bounded: drop scored decisions older than two weeks
    old = str(today - timedelta(days=14))
    st['decisions'] = {k: v for k, v in st['decisions'].items() if not (v.get('scored') and v['date'] < old)}
    st['upcoming'] = upcoming
    st['training_days'] = {u: sum(1 for r in deltas['rows'] if r[1] == u) for u in ('C', 'F')}
    st['mode'] = 'trading'
    st['history'].append({'ts': now.isoformat(), 'equity': round(equity(st), 2),
                          'cash': round(st['cash'], 2), 'open': len(st['open'])})
    st['history'] = st['history'][-3000:]
    st['last_run'] = {k: report[k] for k in ('ts', 'decided', 'skipped')}
    save(d / 'nowcast_state.json', st)
    save(d / 'deltas.json', deltas)
    return report
