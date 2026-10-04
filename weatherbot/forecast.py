"""Multi-model forecasts of the daily high at a settlement station (Open-Meteo).

The daily high is always built the same way, live and in the backtest: the
maximum of the hourly 2 m temperature over the station's local calendar day,
averaged over the models that cover the location.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

import requests

FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'
PREVIOUS_RUNS_URL = 'https://previous-runs-api.open-meteo.com/v1/forecast'
# one model per family (ICON and GEM variants are collapsed by Open-Meteo's
# seamless products), the same set PolyWeather blends
MODELS = ('ecmwf_ifs025', 'ecmwf_aifs025_single', 'gfs_seamless',
          'icon_seamless', 'gem_seamless', 'jma_seamless')
STATIONS = json.loads((Path(__file__).with_name('stations.json')).read_text())


def station_coords(icao: str) -> tuple[float, float] | None:
    s = STATIONS.get((icao or '').upper())
    return (s['lat'], s['lon']) if s else None


def _daily_max(payload: dict, var_prefix: str) -> tuple[dict[date, dict[str, float]], int]:
    """{local_date: {model: max hourly temp}} plus the response's UTC offset."""
    hourly = payload['hourly']
    times = hourly['time']
    out: dict[date, dict[str, float]] = defaultdict(dict)
    for model in MODELS:
        values = hourly.get(f'{var_prefix}_{model}')
        if not values:
            continue
        per_day: dict[date, list[float]] = defaultdict(list)
        for t, v in zip(times, values):
            if v is not None:
                per_day[date.fromisoformat(t[:10])].append(float(v))
        for d, vs in per_day.items():
            if len(vs) >= 20:  # a nearly complete local day
                out[d][model] = max(vs)
    return dict(out), int(payload.get('utc_offset_seconds', 0))


def blend(models: dict[str, float]) -> tuple[float, float, int] | None:
    """Equal-weight mean, spread (max - min) and model count."""
    vals = list(models.values())
    if len(vals) < 3:
        return None
    return sum(vals) / len(vals), max(vals) - min(vals), len(vals)


ENSEMBLE_URL = 'https://ensemble-api.open-meteo.com/v1/ensemble'


def gfs_ensemble(lat: float, lon: float, unit: str, session: requests.Session | None = None,
                 days: int = 3) -> dict[date, list[float]]:
    """Daily high per GFS ensemble member (control + 30), per local date --
    the forecast suislanchez's weather bot counts members of."""
    http = session or requests.Session()
    r = http.get(ENSEMBLE_URL, params={
        'latitude': lat, 'longitude': lon, 'hourly': 'temperature_2m', 'models': 'gfs_seamless',
        'timezone': 'auto', 'forecast_days': days,
        'temperature_unit': 'fahrenheit' if unit == 'F' else 'celsius'}, timeout=30)
    r.raise_for_status()
    hourly = r.json()['hourly']
    out: dict[date, list[float]] = defaultdict(list)
    for key, values in hourly.items():
        if not key.startswith('temperature_2m'):
            continue
        per_day: dict[date, list[float]] = defaultdict(list)
        for t, v in zip(hourly['time'], values):
            if v is not None:
                per_day[date.fromisoformat(t[:10])].append(float(v))
        for d, vs in per_day.items():
            if len(vs) >= 20:
                out[d].append(max(vs))
    return dict(out)


def ensemble_bucket_probs(members: list[float], buckets) -> list[float]:
    """Their method: share of members whose (whole-degree) high falls in the
    bucket, clipped to [0.05, 0.95] as in their weather_signals.py."""
    n = len(members)
    return [min(0.95, max(0.05, sum(b.lo <= round(m) <= b.hi for m in members) / n)) for b in buckets]


def live_forecast(lat: float, lon: float, unit: str, session: requests.Session | None = None,
                  days: int = 3) -> tuple[dict[date, dict[str, float]], int]:
    http = session or requests.Session()
    r = http.get(FORECAST_URL, params={
        'latitude': lat, 'longitude': lon, 'hourly': 'temperature_2m', 'models': ','.join(MODELS),
        'timezone': 'auto', 'forecast_days': days,
        'temperature_unit': 'fahrenheit' if unit == 'F' else 'celsius'}, timeout=30)
    r.raise_for_status()
    return _daily_max(r.json(), 'temperature_2m')


def lead1_history(lat: float, lon: float, unit: str, start: date, end: date,
                  session: requests.Session | None = None) -> tuple[dict[date, dict[str, float]], int]:
    """Forecasts as they stood one day before each hour (previous_day1): what a
    bot trading at 06:00 local could have known, without later model runs."""
    http = session or requests.Session()
    r = http.get(PREVIOUS_RUNS_URL, params={
        'latitude': lat, 'longitude': lon, 'hourly': 'temperature_2m_previous_day1',
        'models': ','.join(MODELS), 'timezone': 'auto',
        'start_date': start.isoformat(), 'end_date': end.isoformat(),
        'temperature_unit': 'fahrenheit' if unit == 'F' else 'celsius'}, timeout=60)
    r.raise_for_status()
    return _daily_max(r.json(), 'temperature_2m_previous_day1')
