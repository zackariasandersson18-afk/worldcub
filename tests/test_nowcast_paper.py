import json
from datetime import date, datetime, timedelta, timezone

from tests.test_weatherbot import FakeWorld, Resp
from weatherbot import forecast, model, nowcast, nowcast_paper


class TextResp(Resp):
    def __init__(self, text):
        super().__init__(None)
        self.text = text


class NowcastWorld(FakeWorld):
    """Madrid (LEMD) on 2026-10-03: 22 C observed before 16:00 solar; the rise after
    16:00 was 0 on 4 of 5 past days. The 22 C bucket is offered at 0.30."""

    def __init__(self, today_high=22, late_obs=None, **kw):
        super().__init__(**kw)
        self.today_high, self.late_obs = today_high, late_obs or []
        self.metar_calls = 0

    def get(self, url, params=None, timeout=None):
        if url == nowcast.IEM:
            self.metar_calls += 1
            lon = forecast.station_coords('LEMD')[1]
            lines = ['station,valid,tmpf']

            def add(day, hour, c):
                t = nowcast.solar_day_start(day, lon) + int(hour * 3600)
                lines.append(f"LEMD,{datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d %H:%M},{c * 9 / 5 + 32:.1f}")
            for k in range(1, 60):
                day = date(2026, 10, 3) - timedelta(days=k)
                add(day, 9, 14)
                add(day, 15, 20)
                add(day, 17, 21 if k % 5 == 0 else 20)
            add(date(2026, 10, 3), 9, 15)
            add(date(2026, 10, 3), 15.5, self.today_high)
            for hour, c in self.late_obs:
                add(date(2026, 10, 3), hour, c)
            return TextResp('\n'.join(lines))
        return super().get(url, params, timeout)


def at_solar(hour):
    lon = forecast.station_coords('LEMD')[1]
    return datetime.fromtimestamp(nowcast.solar_day_start(date(2026, 10, 3), lon) + int(hour * 3600), timezone.utc)


def test_trades_after_16_solar_on_observed_high_and_settles(tmp_path, monkeypatch):
    monkeypatch.setattr(nowcast, 'MIN_TRAIN_DAYS', 50)
    world = NowcastWorld(late_obs=[(16.5, 30)])  # a later observation must not be used
    assert nowcast_paper.step(tmp_path, now=at_solar(15.5), http=world)['opened'] == []  # too early
    rep = nowcast_paper.step(tmp_path, now=at_solar(16.4), http=world)
    bought = {p['bucket']: p for p in rep['opened']}
    assert bought['22°C']['side'] == 'YES' and bought['22°C']['price'] == 0.30
    assert all(p['observed'] == 22 for p in rep['opened'])
    assert all(p['edge'] >= model.MIN_EDGE for p in rep['opened'])
    st = json.loads((tmp_path / 'nowcast_state.json').read_text())
    dec = next(iter(st['decisions'].values()))
    assert dec['observed'] == 22 and abs(dec['probs'][dec['labels'].index('22°C')] - 0.8) < 0.05
    # decided once: a second run in the window does nothing
    assert nowcast_paper.step(tmp_path, now=at_solar(16.9), http=world)['opened'] == []

    world.resolve(winner=22)
    rep = nowcast_paper.step(tmp_path, now=at_solar(30), http=world)
    settled = {p['bucket']: p for p in rep['settled']}
    assert settled['22°C']['won'] and settled['22°C']['pnl_usd'] > 0
    st = json.loads((tmp_path / 'nowcast_state.json').read_text())
    assert not st['open'] and st['score']['events'] == 1


def test_uses_every_report_up_to_the_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(nowcast, 'MIN_TRAIN_DAYS', 50)
    world = NowcastWorld(late_obs=[(16.2, 23), (16.6, 30)])  # 16:12 report seen; 16:36 is in the future
    nowcast_paper.step(tmp_path, now=at_solar(16.4), http=world)
    st = json.loads((tmp_path / 'nowcast_state.json').read_text())
    assert next(iter(st['decisions'].values()))['observed'] == 23


def test_a_city_missed_for_the_whole_hour_is_not_traded_late(tmp_path, monkeypatch):
    monkeypatch.setattr(nowcast, 'MIN_TRAIN_DAYS', 50)
    rep = nowcast_paper.step(tmp_path, now=at_solar(17.2), http=NowcastWorld())
    assert rep['decided'] == 0 and rep['opened'] == []


def test_waits_until_enough_training_days(tmp_path):
    rep = nowcast_paper.step(tmp_path, now=at_solar(16.4), http=NowcastWorld())
    assert rep['opened'] == [] and 'training' in rep['skipped']['Madrid']
