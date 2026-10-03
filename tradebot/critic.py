"""The critic: eight ways your backtest is lying.

Two halves:
  * review(): checks source code line by line against the eight-point
    list and quotes the lines. Status refers to the ERROR: PRESENT means
    the problem is there, ABSENT means it is not, CHECK means a human
    (or the LLM critic prompt in prompts.CRITIC) has to decide.
  * leakage_test(): runs the signal function on truncated and
    scrambled-future data. If the signal at t changes when only data
    after t changes, it is using the future. This is the hard proof.
"""
from __future__ import annotations

import re
from typing import Callable

import numpy as np
import pandas as pd

from tradebot.data import check_alignment
from tradebot.regimes import label_regimes


def _find(lines: list[str], pattern: str) -> list[str]:
    rx = re.compile(pattern)
    return [f'L{n}: {line.strip()}' for n, line in enumerate(lines, 1)
            if rx.search(line) and not line.strip().startswith('#')]


def _item(n: int, name: str, status: str, quotes: list[str]) -> dict:
    return {'item': n, 'name': name, 'status': status, 'quotes': quotes}


def review(source: str, prices: pd.Series | None = None,
           universe: list[str] | None = None,
           delisted: list[str] | None = None,
           params_fitted_out_of_sample: bool | None = None) -> list[dict]:
    lines = source.splitlines()
    out = []

    # 1. Look-ahead
    future = _find(lines, r'\.shift\(\s*-')
    shifted = _find(lines, r'signal\w*\.shift\(\s*[1-9]')
    if future:
        out.append(_item(1, 'Look-ahead', 'PRESENT', future))
    elif shifted:
        out.append(_item(1, 'Look-ahead', 'ABSENT', shifted))
    else:
        out.append(_item(1, 'Look-ahead', 'PRESENT',
                         ['no line shifts the signal before it becomes a position']))

    # 2. Survivorship
    if universe is not None and len(universe) > 1:
        if delisted:
            included = [t for t in delisted if t in universe]
            status = 'ABSENT' if included else 'PRESENT'
            out.append(_item(2, 'Survivorship', status,
                             [f'delisted in universe: {included or "none"}']))
        else:
            out.append(_item(2, 'Survivorship', 'CHECK',
                             [f'universe={universe}; no delisted list supplied']))
    else:
        quotes = _find(lines, r'delist') or [
            'single-asset backtest; survivorship applies to asset selection']
        out.append(_item(2, 'Survivorship', 'CHECK', quotes))

    # 3. Repainting
    repaint = _find(lines, r'center\s*=\s*True|zigzag|bfill|backfill|'
                           r'interpolate\(|\.shift\(\s*-')
    resample = [q for q in _find(lines, r'\.resample\(') if 'shift' not in q]
    hits = repaint + resample
    out.append(_item(3, 'Repainting', 'PRESENT' if hits else 'ABSENT',
                     hits or ['no centered windows, zigzag, backfill or unshifted resample']))

    # 4. Costs
    fees = _find(lines, r'fee')
    slip = _find(lines, r'slippage')
    turnover = _find(lines, r'turnover')
    cost_lines = [q for q in fees if 'slippage' in q and 'turnover' in q] or \
        _find(lines, r'turnover.*\(.*fee.*slippage|turnover.*slippage.*fee')
    if cost_lines:
        out.append(_item(4, 'Costs', 'ABSENT', cost_lines))
    else:
        missing = [n for n, q in (('fees', fees), ('slippage', slip),
                                  ('turnover', turnover)) if not q]
        out.append(_item(4, 'Costs', 'PRESENT',
                         [f'not applied together on turnover; missing: {missing or "a line combining them"}']))

    # 5. Fill assumption
    intrabar = _find(lines, r"\[\s*['\"](high|low|open)['\"]\s*\]|\.(high|low)\b")
    if intrabar:
        out.append(_item(5, 'Fill assumption', 'CHECK', intrabar))
    else:
        fills = _find(lines, r'pct_change|prices\s*/\s*prices\.shift')
        out.append(_item(5, 'Fill assumption', 'ABSENT',
                         fills or ['close-to-close returns only']))

    # 6. Parameter fitting
    params = _find(lines, r'def \w+\(.*=\s*-?\d|^\s*\w+\s*:\s*\w+\s*=\s*-?\d|GRID')
    if params_fitted_out_of_sample is None:
        status = 'CHECK'
    else:
        status = 'ABSENT' if params_fitted_out_of_sample else 'PRESENT'
    out.append(_item(6, 'Parameter fitting', status,
                     [f'{len(params)} parameter line(s)'] + params))

    # 7. Sample
    if prices is not None:
        labels = label_regimes(prices)
        counts = labels.value_counts().to_dict()
        both = counts.get('bull', 0) > 0 and counts.get('bear', 0) > 0
        out.append(_item(7, 'Sample', 'ABSENT' if both else 'PRESENT',
                         [f'bars per regime: {counts}']))
    else:
        out.append(_item(7, 'Sample', 'CHECK', ['no price data supplied']))

    # 8. Data alignment
    if prices is not None:
        problems = check_alignment(prices)
        out.append(_item(8, 'Data alignment', 'PRESENT' if problems else 'ABSENT',
                         problems or [f'index tz={prices.index.tz}, sorted, unique, bar-close stamps']))
    else:
        out.append(_item(8, 'Data alignment', 'CHECK', ['no price data supplied']))

    return out


def format_review(items: list[dict]) -> str:
    rows = []
    for it in items:
        rows.append(f"{it['item']}. {it['name']}: {it['status']}")
        rows.extend(f'     {q}' for q in it['quotes'])
    return '\n'.join(rows)


SignalFn = Callable[[pd.Series], pd.Series]


def leakage_test(signal_fn: SignalFn, prices: pd.Series, n_cuts: int = 8,
                 seed: int = 0) -> dict:
    """The signal at t must not change when data after t changes."""
    rng = np.random.default_rng(seed)
    full = signal_fn(prices)
    n = len(prices)
    cuts = sorted(rng.integers(n // 3, n - 2, size=n_cuts))
    failures = []

    for cut in cuts:
        known = slice(0, cut + 1)
        truncated = signal_fn(prices.iloc[known])

        # replace the future with a different random walk
        tail = prices.iloc[cut] * np.exp(np.cumsum(
            rng.normal(0, 0.05, n - cut - 1)))
        scrambled = prices.copy()
        scrambled.iloc[cut + 1:] = tail
        alt = signal_fn(scrambled)

        # truncated is the ground truth: it cannot have seen anything after t
        truth = truncated.fillna(0).to_numpy()
        for label, other in (('full history', full), ('scrambled future', alt)):
            seen = other.iloc[known].fillna(0).to_numpy()
            diff = np.flatnonzero(~np.isclose(seen, truth))
            if diff.size:
                failures.append(
                    f'signal at {prices.index[diff[0]]} differs from the truncated '
                    f'run when using {label} after {prices.index[cut]}')
                break

    return {
        'status': 'PRESENT' if failures else 'ABSENT',
        'cuts_tested': len(cuts),
        'failures': failures,
    }
