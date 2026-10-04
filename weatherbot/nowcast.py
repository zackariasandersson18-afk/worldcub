"""Late-day nowcast backtest (research round 5).

Idea: by mid-afternoon most of the day's highest temperature has already been
observed at the resolving airport station (METAR). If the market is slow to
absorb the observations, the bucket probabilities can be read off them.

Pre-registered BEFORE any result was seen (+2 trials -> 51):
  data       METAR observations from the Iowa Environmental Mesonet ASOS archive;
             market prices from Polymarket prices-history (last point at or
             before the decision time); settlement = the market's own resolution
  time       local SOLAR time (UTC + longitude / 15): no timezone database needed,
             and the daily temperature cycle follows the sun
  observed   M = highest observation, rounded to whole market degrees, between
             solar 00:00 and the decision hour (strictly before it)
  remaining  delta = (METAR max of the whole solar day) - M, a whole number >= 0.
             Its distribution is learned per unit (C / F), pooled over stations,
             from days STRICTLY before the trading day (expanding window, 30 days
             of warm-up observations before the first market); smoothed as
             (count_d + 0.1) / (N + 0.1 * (K + 1)) for d = 0..K, K = 10 C / 18 F;
             trading starts once a unit has >= 200 training days
  probs      P(bucket) = P(lo <= M + delta <= hi), clipped to [0.01, 0.99]
             (METAR and the resolution source can disagree)
  variants   decision hour 14:00 and 16:00 solar
  bets       unchanged weatherbot.model rules: edge >= 5 points after taker fee
             and 1 point slippage, quarter Kelly, <= 2 % per bet, <= 5 % per city-day
  gates      unchanged: walk-forward 7-day folds (>= 60 % positive, worst >= -2,
             mean > 0) and deflated Sharpe > 0.95 with 51 trials
"""
from __future__ import annotations

import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests

from weatherbot import backtest, forecast, markets, model

N_TRIALS = 51
VARIANTS = (14, 16)
WARMUP_DAYS = 30
MIN_TRAIN_DAYS = 200
K_MAX = {'C': 10, 'F': 18}
SMOOTH = 0.1
P_CLIP = (0.01, 0.99)
IEM = 'https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py'


def to_unit(temp_f: float, unit: str) -> int:
    return round(temp_f) if unit == 'F' else round((temp_f - 32) * 5 / 9)


def solar_offset_s(lon: float) -> int:
    return int(round(lon / 15 * 3600))


def solar_day_start(day: date, lon: float) -> int:
    """Unix time of solar midnight starting `day` at longitude lon."""
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()) - solar_offset_s(lon)


def daily_obs(obs: list[tuple[int, float]], lon: float, unit: str, hour: int) -> dict[date, tuple[int, int]]:
    """{solar day: (max before `hour`, max of whole day)} in whole market degrees.
    Days without any observation before `hour` are dropped."""
    off = solar_offset_s(lon)
    before: dict[date, int] = {}
    full: dict[date, int] = {}
    for t, f in obs:
        local = t + off
        d = datetime.fromtimestamp(local, timezone.utc).date()
        v = to_unit(f, unit)
        full[d] = max(full.get(d, v), v)
        if (local % 86400) < hour * 3600:
            before[d] = max(before.get(d, v), v)
    return {d: (before[d], full[d]) for d in before if d in full}


class DeltaDist:
    """Expanding, per-unit distribution of (day max - max before the decision hour)."""

    def __init__(self):
        self.rows: list[tuple[date, str, int]] = []

    def add(self, day: date, unit: str, delta: int) -> None:
        self.rows.append((day, unit, max(0, delta)))

    def probs(self, before: date, unit: str) -> list[float] | None:
        k = K_MAX[unit]
        ds = [min(d, k) for day, u, d in self.rows if u == unit and day < before]
        if len(ds) < MIN_TRAIN_DAYS:
            return None
        counts = [0] * (k + 1)
        for d in ds:
            counts[d] += 1
        total = len(ds) + SMOOTH * (k + 1)
        return [(c + SMOOTH) / total for c in counts]


def bucket_probs(m: int, dist: list[float], buckets) -> list[float]:
    out = []
    for b in buckets:
        p = sum(q for d, q in enumerate(dist) if b.lo <= m + d <= b.hi)
        out.append(min(P_CLIP[1], max(P_CLIP[0], p)))
    return out


def price_age(history, ts: int) -> int | None:
    """Minutes between the price point price_at would use and the decision time."""
    before = [t for t, _ in history if t <= ts]
    return (ts - before[-1]) // 60 if before else None


def run(events: list[markets.Event], metar: dict, histories: dict, hour: int,
        max_age_s: int = 6 * 3600) -> dict:
    """metar: {station: [(unix, temp_f), ...]}; histories: {yes_token: [(t, p), ...]}"""
    units = {}
    for e in events:
        units.setdefault(e.station, e.unit)
    by_station = {}
    for st, obs in metar.items():
        coords = forecast.station_coords(st)
        if coords and st in units:
            by_station[st] = (coords[1], daily_obs(obs, coords[1], units[st], hour))
    ev_by_key = {(e.station, e.date): e for e in events}
    all_days = sorted({d for _, days in by_station.values() for d in days})
    dist = DeltaDist()
    trades, scored, daily = [], [], {}
    for day in all_days:
        pnl, traded_day, todays = 0.0, False, []
        for st, (lon, days) in by_station.items():
            if day not in days:
                continue
            m, full = days[day]
            todays.append((units[st], full - m))
            ev = ev_by_key.get((st, day))
            if ev is None or ev.winner is None:
                continue
            dp = dist.probs(before=day, unit=ev.unit)
            if dp is None:
                continue
            traded_day = True
            probs = bucket_probs(m, dp, ev.buckets)
            ts = solar_day_start(day, lon) + hour * 3600
            prices = [markets.price_at(histories.get(b.yes_token, []), ts, max_age_s) for b in ev.buckets]
            for b, p, q in zip(ev.buckets, probs, prices):
                if q is not None:
                    scored.append({'won': bool(b.resolved_yes), 'model': p, 'market': q})
            for bet in model.select_bets(ev.buckets, probs, prices):
                b = next(x for x in ev.buckets if x.label == bet.bucket)
                won = bool(b.resolved_yes) if bet.side == 'YES' else not b.resolved_yes
                r = model.settle(bet, won)
                pnl += r
                trades.append({'date': str(day), 'city': ev.city, 'station': st, 'observed': m,
                               'bucket': bet.bucket, 'side': bet.side, 'price': bet.price,
                               'cost': round(bet.cost, 4), 'prob': round(bet.prob, 4),
                               'edge': round(bet.edge, 4), 'stake': round(bet.stake, 5),
                               'won': won, 'pnl': round(r, 5),
                               'price_age_min': price_age(histories.get(b.yes_token, []), ts),
                               # already settled by the observations: the high is past this bucket
                               'decided': bool(b.hi < m)})
        if traded_day:
            daily[day] = pnl
        for unit, delta in todays:  # only now: today's full-day max is known
            dist.add(day, unit, delta)
    out = backtest.evaluate(trades, scored, daily, n_trials=N_TRIALS)
    out['decision_hour'] = hour
    return out


def fetch_metar(station: str, start: date, end: date, http: requests.Session) -> list[tuple[int, float]]:
    """All routine + special METARs (UTC) between start and end inclusive."""
    ids = [station, station[1:]] if station.startswith('K') and len(station) == 4 else [station]
    for sid in ids:
        r = http.get(IEM, params={
            'station': sid, 'data': 'tmpf', 'tz': 'Etc/UTC', 'format': 'onlycomma', 'latlon': 'no',
            'missing': 'M', 'trace': 'T', 'direct': 'no', 'report_type': [3, 4],
            'year1': start.year, 'month1': start.month, 'day1': start.day,
            'year2': (end + timedelta(days=1)).year, 'month2': (end + timedelta(days=1)).month,
            'day2': (end + timedelta(days=1)).day}, timeout=120)
        r.raise_for_status()
        obs = []
        for row in csv.DictReader(io.StringIO(r.text)):
            try:
                t = int(datetime.strptime(row['valid'], '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc).timestamp())
                obs.append((t, float(row['tmpf'])))
            except (KeyError, ValueError):
                continue
        if obs:
            return obs
    return []


def collect(days: int = 45, pages: int = 40, workers: int = 8, log=print):
    http = requests.Session()
    cutoff = date.today() - timedelta(days=days)
    events = [e for e in backtest._retry(markets.fetch_events, True, pages, 100, http, cutoff)
              if e.date >= cutoff and e.winner and forecast.station_coords(e.station)]
    log(f'{len(events)} resolved events since {cutoff} with a known station')
    stations = sorted({e.station for e in events})
    metar = {}
    for st in stations:
        try:
            metar[st] = backtest._retry(fetch_metar, st, cutoff - timedelta(days=WARMUP_DAYS),
                                        date.today(), http)
        except Exception as exc:
            log(f'metar {st}: {exc}')
    log(f'METAR for {sum(1 for v in metar.values() if v)}/{len(stations)} stations, '
        f'{sum(len(v) for v in metar.values())} observations')
    tokens = [b.yes_token for e in events for b in e.buckets]
    histories = {}
    with ThreadPoolExecutor(workers) as pool:
        for tok, hist in zip(tokens, pool.map(lambda t: backtest._safe_history(t, http), tokens)):
            histories[tok] = hist
    log(f'price histories for {sum(1 for h in histories.values() if h)}/{len(tokens)} buckets')
    return events, metar, histories


def diagnose(t) -> None:
    """Where does the profit come from? Stale prices on already-decided buckets
    are not executable: the order book moves as soon as the observation is out."""
    import pandas as pd
    age = pd.cut(t['price_age_min'], [-1, 30, 60, 120, 360], labels=['<=30m', '30-60m', '1-2h', '2-6h'])
    print('\nby price age:\n', t.groupby(age, observed=True)['pnl'].agg(['count', 'sum']).round(3))
    print('\nby already-decided bucket:\n', t.groupby('decided')[['won', 'pnl']].agg(['count', 'mean', 'sum']).round(3))


def main(argv=None) -> int:
    import argparse
    import pandas as pd
    ap = argparse.ArgumentParser(prog='weatherbot.nowcast')
    ap.add_argument('--days', type=int, default=45)
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    events, metar, histories = collect(a.days)
    results = {}
    for hour in VARIANTS:
        res = run(events, metar, histories, hour)
        trades = res.pop('trades')
        print(f'\n===== decision hour {hour}:00 solar =====')
        print(json.dumps(res, indent=2, default=str))
        if trades:
            t = pd.DataFrame(trades)
            print(t.groupby('side')[['won', 'pnl']].agg(['count', 'mean', 'sum']).round(4))
            diagnose(t)
        results[hour] = {**res, 'trades': trades}
    # robustness (NOT new trials, only stricter): the hour-16 rule with fresh prices only
    for age in (3600, 1800):
        res = run(events, metar, histories, 16, max_age_s=age)
        res.pop('trades')
        print(f'\n----- robustness: hour 16, price at most {age // 60} min old -----')
        print(json.dumps({k: res.get(k) for k in ('n_trades', 'hit_rate', 'total_return', 'logloss_model',
                                                   'logloss_market', 'folds', 'gates', 'verdict')}, default=str))
    if a.out:
        with open(a.out, 'w') as f:
            json.dump(results, f, indent=1, default=str)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
