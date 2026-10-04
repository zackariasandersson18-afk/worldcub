"""Walk-forward backtest on closed Polymarket highest-temperature events.

For every event, in date order:
  1. forecast  = Open-Meteo previous_day1 (runs available before 06:00 local)
  2. price     = last market price at or before 06:00 local on the day
  3. calibration (bias, sigma) from residuals of strictly earlier days only
  4. bets per weatherbot.model, settled with the event's actual resolution
Only after a day is fully processed are its residuals added.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

from tradebot.engine import Config, metrics
from tradebot.stats import deflated_sharpe
from weatherbot import forecast, markets, model

N_TRIALS = 48           # 47 earlier + this pre-registered configuration
FOLD_DAYS = 7


def _retry(fn, *args, tries: int = 4):
    for i in range(tries):
        try:
            return fn(*args)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code not in (429, 500, 502, 503, 504):
                raise
        except requests.RequestException:
            pass
        time.sleep(2 ** i)
    return fn(*args)


def run(events: list[markets.Event], forecasts: dict, histories: dict) -> dict:
    """forecasts: {(station, unit): (daily {date: {model: high}}, utc_offset)};
    histories: {yes_token: [(t, p), ...]}"""
    calib = model.Calibration()
    by_day: dict[date, list[markets.Event]] = defaultdict(list)
    for ev in events:
        by_day[ev.date].append(ev)

    trades, scored, daily = [], [], {}
    for day in sorted(by_day):
        pnl, residuals = 0.0, []
        for ev in by_day[day]:
            fc = forecasts.get((ev.station, ev.unit))
            winner = ev.winner
            if not fc or winner is None:
                continue
            daily_fc, offset = fc
            blended = forecast.blend(daily_fc.get(day, {}))
            if not blended:
                continue
            mu_raw, spread, n_models = blended
            residuals.append((winner.center() - mu_raw, ev.unit))

            cal = calib.params(before=day, unit=ev.unit)
            if cal is None:
                continue
            bias, sigma = cal
            mu = mu_raw + bias
            probs = [model.bucket_prob(mu, sigma, b.lo, b.hi) for b in ev.buckets]
            ts = markets.decision_ts(day, offset)
            prices = [markets.price_at(histories.get(b.yes_token, []), ts) for b in ev.buckets]
            for b, p, q in zip(ev.buckets, probs, prices):
                if q is not None:
                    scored.append({'won': bool(b.resolved_yes), 'model': p, 'market': q})
            for bet in model.select_bets(ev.buckets, probs, prices):
                b = next(x for x in ev.buckets if x.label == bet.bucket)
                won = bool(b.resolved_yes) if bet.side == 'YES' else not b.resolved_yes
                r = model.settle(bet, won)
                pnl += r
                trades.append({'date': str(day), 'city': ev.city, 'station': ev.station,
                               'bucket': bet.bucket, 'side': bet.side, 'price': bet.price,
                               'cost': round(bet.cost, 4), 'prob': round(bet.prob, 4),
                               'edge': round(bet.edge, 4), 'stake': round(bet.stake, 5),
                               'won': won, 'pnl': round(r, 5), 'mu': round(mu, 2),
                               'sigma': round(sigma, 2), 'models': n_models})
        if calib.params(before=day, unit='C') is not None:
            daily[day] = pnl
        for res, unit in residuals:  # only now: today's outcome is known
            calib.add(day, res, unit)
    return evaluate(trades, scored, daily)


def _logloss(rows, key):
    eps = 1e-4
    return float(np.mean([-math.log(min(max(r[key] if r['won'] else 1 - r[key], eps), 1)) for r in rows]))


def evaluate(trades: list[dict], scored: list[dict], daily: dict) -> dict:
    out = {'n_trades': len(trades), 'n_days': len(daily), 'trades': trades}
    if scored:
        # does the model know more than the market price? lower log loss = better
        out['logloss_model'] = round(_logloss(scored, 'model'), 4)
        out['logloss_market'] = round(_logloss(scored, 'market'), 4)
        out['n_scored'] = len(scored)
    if not trades or len(daily) < 2 * FOLD_DAYS:
        out['verdict'] = 'NOT ENOUGH DATA'
        return out
    t = pd.DataFrame(trades)
    out.update({'hit_rate': round(float(t['won'].mean()), 3), 'avg_edge': round(float(t['edge'].mean()), 3),
                'avg_price': round(float(t['price'].mean()), 3),
                'total_return': round(float(t['pnl'].sum()) * 100, 2),
                'yes_share': round(float((t['side'] == 'YES').mean()), 2)})
    rets = pd.Series(daily).sort_index()
    rets.index = pd.to_datetime(rets.index)
    cfg = Config(periods_per_year=365)
    m = metrics(rets, cfg, min_obs=FOLD_DAYS)
    out['metrics'] = m
    folds = [rets.iloc[i:i + FOLD_DAYS] for i in range(0, len(rets) - FOLD_DAYS + 1, FOLD_DAYS)]
    fold_sharpes = [metrics(f, cfg, min_obs=FOLD_DAYS).get('sharpe', 0.0) for f in folds]
    pos = sum(s > 0 for s in fold_sharpes) / len(fold_sharpes)
    out['folds'] = {'n': len(folds), 'positive': f'{sum(s > 0 for s in fold_sharpes)}/{len(folds)}',
                    'worst': round(min(fold_sharpes), 2), 'mean': round(float(np.mean(fold_sharpes)), 2)}
    dsr = deflated_sharpe(m['sharpe'], N_TRIALS, m['n_obs'], m['skew'], m['kurtosis'], 365)
    out['dsr'] = dsr
    gates = {
        'no_leakage': True,  # structural: prices <= decision time, calibration < day, previous_day1 runs
        'deflated_sharpe': dsr['deflated_sharpe'] > 0.95,
        'walk_forward': pos >= 0.6 and min(fold_sharpes) >= -2 and np.mean(fold_sharpes) > 0,
    }
    out['gates'] = gates
    out['approved'] = all(gates.values())
    out['verdict'] = 'APPROVED' if out['approved'] else 'REJECTED'
    return out


def collect(days: int = 45, pages: int = 40, workers: int = 8, session: requests.Session | None = None,
            log=print) -> tuple[list, dict, dict]:
    http = session or requests.Session()
    cutoff = date.today() - timedelta(days=days)
    events = [e for e in _retry(markets.fetch_events, True, pages, 100, http)
              if e.date >= cutoff and e.winner and forecast.station_coords(e.station)]
    log(f'{len(events)} resolved events since {cutoff} with a known station')

    groups: dict[tuple, list[date]] = defaultdict(list)
    for e in events:
        groups[(e.station, e.unit)].append(e.date)
    forecasts = {}
    for (station, unit), ds in groups.items():
        lat, lon = forecast.station_coords(station)
        try:
            forecasts[(station, unit)] = _retry(forecast.lead1_history, lat, lon, unit,
                                                min(ds), max(ds), http)
        except Exception as exc:
            log(f'forecast {station}: {exc}')
    log(f'forecasts for {len(forecasts)}/{len(groups)} stations')

    tokens = [b.yes_token for e in events for b in e.buckets]
    histories = {}
    with ThreadPoolExecutor(workers) as pool:
        for tok, hist in zip(tokens, pool.map(lambda t: _safe_history(t, http), tokens)):
            histories[tok] = hist
    log(f'price histories for {sum(1 for h in histories.values() if h)}/{len(tokens)} buckets')
    return events, forecasts, histories


def _safe_history(token, http):
    try:
        return _retry(markets.price_history, token, http)
    except Exception:
        return []
