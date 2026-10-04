from datetime import date, datetime, timedelta, timezone

import numpy as np

from weatherbot import markets, nowcast
from tests.test_weatherbot import gamma_event


def ts(day, hour, lon=0.0):
    return nowcast.solar_day_start(day, lon) + int(hour * 3600)


def test_daily_obs_only_counts_observations_before_the_decision_hour():
    d = date(2026, 9, 1)
    obs = [(ts(d, 9), 68.0), (ts(d, 13.5), 77.0), (ts(d, 14), 86.0), (ts(d, 17), 80.0)]
    out = nowcast.daily_obs(obs, 0.0, 'C', 14)
    assert out[d] == (25, 30)  # 77F -> 25C before 14:00; 86F -> 30C (at 14:00, not before)


def test_solar_time_shifts_with_longitude():
    d = date(2026, 9, 1)
    # lon 30E: solar midnight is 22:00 UTC the evening before
    assert nowcast.solar_day_start(d, 30.0) == int(datetime(2026, 8, 31, 22, tzinfo=timezone.utc).timestamp())


def test_delta_distribution_uses_only_earlier_days_and_needs_enough_data():
    dd = nowcast.DeltaDist()
    start = date(2026, 8, 1)
    for i in range(nowcast.MIN_TRAIN_DAYS):
        dd.add(start, 'C', 0 if i % 2 else 1)
    assert dd.probs(before=start, unit='C') is None          # same day: not usable yet
    p = dd.probs(before=start + timedelta(days=1), unit='C')
    assert abs(sum(p) - 1) < 1e-9 and abs(p[0] - p[1]) < 1e-9 and p[0] > 0.45
    assert dd.probs(before=start + timedelta(days=1), unit='F') is None


def test_bucket_probs_put_no_mass_below_what_was_already_observed():
    e = markets.parse_event(gamma_event(temps=range(17, 28), winner=22))
    dist = [0.6, 0.3, 0.1] + [0.0] * 8
    p = nowcast.bucket_probs(22, dist, e.buckets)
    by = {b.lo: q for b, q in zip(e.buckets, p)}
    assert by[21] == nowcast.P_CLIP[0] and abs(by[22] - 0.6) < 1e-9 and abs(by[23] - 0.3) < 1e-9


def world(market_reacts: bool, n_days=40, seed=0):
    """Afternoon high is mostly known by 14:00; a slow market still prices the morning view."""
    rng = np.random.default_rng(seed)
    events, metar, hist = [], {}, {}
    stations = ['LEMD', 'LFPB', 'EGLC', 'EDDM', 'EHAM', 'EFHK', 'EPWA']
    start = date(2026, 8, 1)
    for st in stations:
        obs = []
        for k in range(-35, n_days):
            day = start + timedelta(days=k)
            high = 20 + int(rng.integers(-3, 4))
            rise = int(rng.choice([0, 0, 0, 1, 2]))
            morning = high - rise
            for h, c in ((8, morning - 6), (13, morning), (16, high)):
                obs.append((ts(day, h), c * 9 / 5 + 32))
            if k < 0:
                continue
            e = markets.parse_event(gamma_event(st, day.day, temps=range(15, 26), winner=high))
            e.date, e.station = day, st
            for i, b in enumerate(e.buckets):
                b.yes_token = f'{st}{day}{i}'
                if market_reacts:
                    fair = {0: .6, 1: .2, 2: .2}.get(int(b.lo) - morning, 0.0) if not np.isinf(b.lo) else 0.0
                else:
                    fair = 1 / 11
                price = float(np.clip(fair, 0.01, 0.99))
                hist[b.yes_token] = [(ts(day, 13), price), (ts(day, 17), 0.99 if b.resolved_yes else 0.01)]
            events.append(e)
        metar[st] = obs
    return events, metar, hist


def test_nowcast_profits_from_a_slow_market_without_seeing_later_prices():
    events, metar, hist = world(market_reacts=False)
    res = nowcast.run(events, metar, hist, 14)
    assert res['n_trades'] > 20 and res['total_return'] > 0
    assert res['logloss_model'] < res['logloss_market']


def test_nowcast_finds_little_when_the_market_already_knows():
    events, metar, hist = world(market_reacts=True, seed=1)
    res = nowcast.run(events, metar, hist, 14)
    assert res.get('total_return', 0) <= 0.5
