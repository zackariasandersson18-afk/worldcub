import json
import math
from datetime import date, timedelta

import numpy as np
import pytest

from weatherbot import backtest, forecast, markets, model


def gamma_event(city='Madrid', day=3, unit='C', temps=range(17, 28), winner=22, closed=True,
                source='https://www.weather.gov/wrh/timeseries?site=lemd'):
    temps = list(temps)
    ms = []
    for i, t in enumerate(temps):
        if i == 0:
            title = f'{t}°{unit} or below'
        elif i == len(temps) - 1:
            title = f'{t}°{unit} or higher'
        else:
            title = f'{t}°{unit}'
        won = t == winner
        ms.append({'groupItemTitle': title, 'clobTokenIds': json.dumps([f'y{city}{day}{t}', f'n{city}{day}{t}']),
                   'outcomePrices': json.dumps(['1', '0'] if won else ['0', '1']) if closed else None,
                   'closed': closed, 'feeSchedule': {'rate': 0.05}, 'bestBid': None, 'bestAsk': 0.001})
    slug_city = city.lower().replace(' ', '-')
    return {'id': f'{city}{day}', 'title': f'Highest temperature in {city} on October {day}?',
            'slug': f'highest-temperature-in-{slug_city}-on-october-{day}-2026', 'closed': closed,
            'resolutionSource': source, 'markets': ms}


def test_parse_real_shaped_event():
    ev = markets.parse_event(gamma_event())
    assert (ev.city, ev.date, ev.station, ev.unit) == ('Madrid', date(2026, 10, 3), 'LEMD', 'C')
    assert ev.buckets[0].lo == -math.inf and ev.buckets[-1].hi == math.inf
    assert ev.winner.label == '22°C' and ev.winner.center() == 22


def test_parse_fahrenheit_ranges_and_wunderground_station():
    e = gamma_event('San Francisco', unit='F', temps=range(75, 95, 2), winner=89,
                    source='https://www.wunderground.com/history/daily/us/ca/KSFO')
    for m in e['markets'][1:-1]:
        lo = int(m['groupItemTitle'].split('°')[0])
        m['groupItemTitle'] = f'{lo}-{lo + 1}°F'
    ev = markets.parse_event(e)
    assert ev.unit == 'F' and ev.station == 'KSFO'
    assert (ev.buckets[1].lo, ev.buckets[1].hi) == (77, 78)


def test_lowest_temperature_and_garbage_are_skipped():
    e = gamma_event()
    e['title'] = 'Lowest temperature in Madrid on October 3?'
    assert markets.parse_event(e) is None
    e = gamma_event()
    e['markets'][3]['groupItemTitle'] = 'something else'
    assert markets.parse_event(e) is None


def test_price_at_never_looks_ahead():
    hist = [(100, 0.2), (200, 0.3), (300, 0.9)]
    assert markets.price_at(hist, 250) == 0.3
    assert markets.price_at(hist, 50) is None
    assert markets.price_at(hist, 10_000_000) is None  # too stale


def test_bucket_probabilities_sum_to_one():
    ev = markets.parse_event(gamma_event())
    ps = [model.bucket_prob(21.3, 1.4, b.lo, b.hi) for b in ev.buckets]
    assert sum(ps) == pytest.approx(1.0)
    assert max(range(len(ps)), key=ps.__getitem__) == [b.label for b in ev.buckets].index('21°C')


def test_select_bets_respects_edge_fees_and_caps():
    ev = markets.parse_event(gamma_event())
    probs = [0.0] * len(ev.buckets)
    probs[5] = 0.6                      # 22 C
    prices = [0.01] * len(ev.buckets)
    prices[5] = 0.30                    # market underprices 22 C
    bets = model.select_bets(ev.buckets, probs, prices)
    yes = [b for b in bets if b.side == 'YES']
    assert len(yes) == 1 and yes[0].bucket == '22°C'
    assert yes[0].cost == pytest.approx(0.30 + 0.01 + 0.05 * 0.30)
    assert sum(b.stake for b in bets) <= model.MAX_EVENT + 1e-12
    assert all(b.stake <= model.MAX_BET for b in bets)
    # a fair price gives no bet
    assert not model.select_bets(ev.buckets[5:6], [0.3], [0.3])


def test_settle():
    bet = model.Bet('x', 'YES', 0.3, 0.325, 0.6, 0.275, 0.02)
    assert model.settle(bet, True) == pytest.approx(0.02 / 0.325 - 0.02)
    assert model.settle(bet, False) == pytest.approx(-0.02)


def test_calibration_uses_only_earlier_days():
    c = model.Calibration()
    d0 = date(2026, 9, 1)
    for i in range(60):
        c.add(d0 + timedelta(days=i // 10), 1.0 if i % 2 else -1.0, 'C')
    assert c.params(before=d0, unit='C') is None
    bias, sigma = c.params(before=d0 + timedelta(days=6), unit='C')
    assert bias == pytest.approx(0, abs=1e-9) and sigma == pytest.approx(1.0, rel=0.05)
    _, sigma_f = c.params(before=d0 + timedelta(days=6), unit='F')
    assert sigma_f == pytest.approx(sigma * 1.8)


def test_daily_max_uses_local_days_and_skips_partial_days():
    times = [f'2026-10-0{1 + h // 24}T{h % 24:02d}:00' for h in range(30)]
    payload = {'utc_offset_seconds': 7200, 'hourly': {
        'time': times, 'temperature_2m_ecmwf_ifs025': list(range(30)),
        'temperature_2m_gfs_seamless': [None] * 30}}
    daily, off = forecast._daily_max(payload, 'temperature_2m')
    assert off == 7200 and list(daily) == [date(2026, 10, 1)]
    assert daily[date(2026, 10, 1)] == {'ecmwf_ifs025': 23.0}


def synthetic_world(n_days=40, cities=('Madrid', 'Paris', 'London'), market_noise=0.25, seed=0):
    """Truth ~ N(forecast, 1.2). The market price is the true probability blurred
    by noise: a model that knows the forecast should beat it."""
    rng = np.random.default_rng(seed)
    events, forecasts, histories = [], {}, {}
    start = date(2026, 8, 1)
    for ci, city in enumerate(cities):
        station = ['LEMD', 'LFPB', 'EGLC'][ci]
        daily = {}
        for k in range(n_days):
            day = start + timedelta(days=k)
            f = 22 + 4 * math.sin(k / 5 + ci)
            daily[day] = {m: f + rng.normal(0, 0.3) for m in forecast.MODELS}
            true_high = round(f + rng.normal(0, 1.2))
            temps = range(round(f) - 5, round(f) + 6)
            e = markets.parse_event(gamma_event(city, day.day, winner=min(max(true_high, temps[0]), temps[-1]),
                                                temps=temps))
            e.date, e.station = day, station
            for tok_suffix, b in enumerate(e.buckets):
                b.yes_token = f'{city}-{day}-{tok_suffix}'
                fair = model.bucket_prob(f, 1.2, b.lo, b.hi)
                noisy = float(np.clip(fair + rng.normal(0, market_noise * max(fair, 0.05)), 0.01, 0.99))
                ts = markets.decision_ts(day, 0)
                histories[b.yes_token] = [(ts - 3600, noisy), (ts + 3600, 0.99)]  # later price must be ignored
            events.append(e)
        forecasts[(station, 'C')] = (daily, 0)
    return events, forecasts, histories


def test_backtest_runs_walk_forward_and_profits_from_real_information():
    events, forecasts, histories = synthetic_world()
    res = backtest.run(events, forecasts, histories)
    assert res['n_trades'] > 20
    assert res['logloss_model'] < res['logloss_market']
    assert res['total_return'] > 0
    assert set(res['gates']) == {'no_leakage', 'deflated_sharpe', 'walk_forward'}
    # trading only starts after enough residuals from earlier days
    first_trade = min(t['date'] for t in res['trades'])
    assert first_trade > str(date(2026, 8, 1) + timedelta(days=10))


def test_backtest_finds_nothing_when_market_is_efficient():
    events, forecasts, histories = synthetic_world(market_noise=0.0, seed=3)
    res = backtest.run(events, forecasts, histories)
    assert res['n_trades'] <= 5  # fair prices + fees leave no edge
