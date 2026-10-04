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


# ---------------------------------------------------------------- paper trading
class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)

    def json(self):
        return self.data


class FakeWorld:
    """Polymarket + Open-Meteo for one Madrid event on 2026-10-03."""

    def __init__(self, forecast_high=22.0, cheap_bucket='22°C', cheap_ask=0.30):
        self.event = gamma_event(closed=False)
        self.forecast_high, self.cheap_bucket, self.cheap_ask = forecast_high, cheap_bucket, cheap_ask

    def resolve(self, winner=22):
        self.event = gamma_event(winner=winner, closed=True)

    def get(self, url, params=None, timeout=None):
        params = params or {}
        if url.endswith('/events'):
            return Resp([self.event] if not self.event['closed'] else [])
        if '/events/' in url:
            return Resp(self.event)
        if url.endswith('/book'):
            tok = params['token_id']
            label = next(m['groupItemTitle'] for m in self.event['markets']
                         if tok in json.loads(m['clobTokenIds']))
            is_yes = tok.startswith('y')
            ask = (self.cheap_ask if is_yes else 1 - self.cheap_ask + 0.02) if label == self.cheap_bucket \
                else (0.02 if is_yes else 0.99)
            return Resp({'bids': [], 'asks': [{'price': str(ask), 'size': '1000'}]})
        if 'open-meteo' in url:
            times = [f'2026-10-0{3 + h // 24}T{h % 24:02d}:00' for h in range(48)]
            temps = [self.forecast_high - 6 + 6 * (12 <= h % 24 <= 16) for h in range(48)]
            return Resp({'utc_offset_seconds': 7200, 'hourly': {
                'time': times, **{f'temperature_2m_{m}': temps for m in forecast.MODELS}}})
        raise AssertionError(url)


def seed_calibration(d, n=60, sigma=1.0):
    rows = [[str(date(2026, 9, 1) + timedelta(days=i % 30)), (1 if i % 2 else -1) * sigma] for i in range(n)]
    (d / 'calibration.json').write_text(json.dumps({'rows': rows}))


def test_paper_buys_underpriced_bucket_at_the_ask_and_settles(tmp_path):
    from datetime import datetime, timezone
    from weatherbot import paper
    seed_calibration(tmp_path)
    world = FakeWorld()
    morning = datetime(2026, 10, 3, 4, 30, tzinfo=timezone.utc)  # 06:30 in Madrid
    rep = paper.step(tmp_path, now=morning, http=world)
    # 22 C at 0.30 is underpriced; so are its neighbours, offered at 0.02
    bought = {p['bucket']: p for p in rep['opened']}
    assert bought['22°C']['side'] == 'YES' and bought['22°C']['price'] == 0.30
    assert set(bought) == {'21°C', '22°C', '23°C'}
    assert all(p['edge'] >= model.MIN_EDGE for p in rep['opened'])
    st = json.loads((tmp_path / 'weather_state.json').read_text())
    assert st['cash'] < paper.START_CASH
    assert sum(p['stake_usd'] for p in st['open']) <= model.MAX_EVENT * paper.START_CASH + 1e-6

    # the same event is never traded twice
    assert paper.step(tmp_path, now=morning.replace(hour=5), http=world)['opened'] == []

    world.resolve(winner=22)
    rep = paper.step(tmp_path, now=datetime(2026, 10, 4, 10, tzinfo=timezone.utc), http=world)
    settled = {p['bucket']: p for p in rep['settled']}
    assert settled['22°C']['won'] and settled['22°C']['pnl_usd'] > 0
    assert not settled['21°C']['won'] and settled['21°C']['pnl_usd'] < 0
    st = json.loads((tmp_path / 'weather_state.json').read_text())
    assert not st['open'] and st['forecast_log'] == {}
    cal = json.loads((tmp_path / 'calibration.json').read_text())['rows']
    assert len(cal) == 61  # the resolved event's residual was learned


def test_paper_waits_for_the_trading_window(tmp_path):
    from datetime import datetime, timezone
    from weatherbot import paper
    seed_calibration(tmp_path)
    night = datetime(2026, 10, 2, 22, tzinfo=timezone.utc)  # 00:00 local: too early
    rep = paper.step(tmp_path, now=night, http=FakeWorld())
    assert rep['opened'] == [] and rep['logged'] == 0


def test_paper_without_approval_only_logs_forecasts(tmp_path):
    from datetime import datetime, timezone
    from weatherbot import paper
    seed_calibration(tmp_path)
    rep = paper.step(tmp_path, now=datetime(2026, 10, 3, 4, 30, tzinfo=timezone.utc),
                     http=FakeWorld(), approved=False)
    assert rep['opened'] == [] and rep['logged'] == 1
    assert rep['skipped']['Madrid'] == 'strategy not approved'


def test_fetch_events_stops_at_since_and_at_gamma_offset_limit():
    pages = {0: [gamma_event(day=3)], 1: [gamma_event(day=2)], 2: [gamma_event(day=1)]}

    class Http:
        calls = 0

        def get(self, url, params, timeout):
            Http.calls += 1
            page = params['offset'] // params['limit']
            return Resp(pages[page] if page in pages else {'error': 'offset'}, 200 if page in pages else 422)
    evs = markets.fetch_events(True, pages=10, page_size=1, session=Http())
    assert [e.date.day for e in evs] == [3, 2, 1]          # 422 on page 3 ends the scan
    Http.calls = 0
    evs = markets.fetch_events(True, pages=10, page_size=1, session=Http(), since=date(2026, 10, 3))
    assert Http.calls == 2 and evs[0].date.day == 3       # page with day 2 < since ends it
