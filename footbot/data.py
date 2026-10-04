"""Free data from football-data.co.uk.

Main leagues (mmz4281/<season>/<code>.csv): results, corners, Pinnacle odds both
when the file is compiled before the round (PSH/PSD/PSA, P>2.5/P<2.5) and at
closing (PSCH/..., PC>2.5/PC<2.5).
Extra leagues (new/<code>.csv, one file for all seasons): results and closing
1X2 odds only.
"""
from __future__ import annotations

import io

import pandas as pd
import requests

BASE = 'https://www.football-data.co.uk'
MAIN = ['E0', 'E1', 'E2', 'E3', 'EC', 'SC0', 'SC1', 'SC2', 'SC3', 'D1', 'D2', 'I1', 'I2',
        'SP1', 'SP2', 'F1', 'F2', 'N1', 'B1', 'P1', 'T1', 'G1']
EXTRA = ['ARG', 'AUT', 'BRA', 'CHN', 'DNK', 'FIN', 'IRL', 'JPN', 'MEX', 'NOR', 'POL', 'ROU',
         'RUS', 'SWE', 'SWZ', 'USA']
SEASONS = ['1819', '1920', '2021', '2122', '2223', '2324', '2425', '2526']

COLS = {  # our name -> football-data column
    'odds_H': 'PSH', 'odds_D': 'PSD', 'odds_A': 'PSA', 'odds_O2.5': 'P>2.5', 'odds_U2.5': 'P<2.5',
    'close_H': 'PSCH', 'close_D': 'PSCD', 'close_A': 'PSCA', 'close_O2.5': 'PC>2.5', 'close_U2.5': 'PC<2.5',
    'hc': 'HC', 'ac': 'AC'}


def _read(text: str) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(text), on_bad_lines='skip')


def _get(url: str, http) -> str | None:
    r = http.get(url, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content.decode('latin-1')


def tidy_main(df: pd.DataFrame, league: str, season: str) -> pd.DataFrame:
    df = df.dropna(subset=['HomeTeam', 'AwayTeam', 'FTHG', 'FTAG'])
    out = pd.DataFrame({
        'league': league, 'season': season,
        'date': pd.to_datetime(df['Date'], dayfirst=True, errors='coerce', format='mixed'),
        'home': df['HomeTeam'].astype(str).str.strip(), 'away': df['AwayTeam'].astype(str).str.strip(),
        'hg': df['FTHG'].astype(int), 'ag': df['FTAG'].astype(int)})
    for ours, theirs in COLS.items():
        out[ours] = pd.to_numeric(df[theirs], errors='coerce') if theirs in df else float('nan')
    return out.dropna(subset=['date'])


def tidy_extra(df: pd.DataFrame, league: str) -> pd.DataFrame:
    df = df.dropna(subset=['Home', 'Away', 'HG', 'AG'])
    out = pd.DataFrame({
        'league': league, 'season': df['Season'].astype(str),
        'date': pd.to_datetime(df['Date'], dayfirst=True, errors='coerce', format='mixed'),
        'home': df['Home'].astype(str).str.strip(), 'away': df['Away'].astype(str).str.strip(),
        'hg': df['HG'].astype(int), 'ag': df['AG'].astype(int)})
    for k in COLS:
        out[k] = float('nan')
    # only closing odds exist here: the backtest bets them (the sharpest price, a harder test)
    for s in 'HDA':
        out[f'close_{s}'] = pd.to_numeric(df.get(f'PSC{s}'), errors='coerce')
    return out.dropna(subset=['date'])


def load(http=None, log=print) -> pd.DataFrame:
    http = http or requests.Session()
    frames = []
    for code in MAIN:
        for season in SEASONS:
            try:
                text = _get(f'{BASE}/mmz4281/{season}/{code}.csv', http)
            except requests.RequestException as exc:
                log(f'{code} {season}: {exc}')
                continue
            if text:
                frames.append(tidy_main(_read(text), code, season))
    for code in EXTRA:
        try:
            text = _get(f'{BASE}/new/{code}.csv', http)
        except requests.RequestException as exc:
            log(f'{code}: {exc}')
            continue
        if text:
            df = tidy_extra(_read(text), code)
            frames.append(df[df['date'] >= '2018-07-01'])
    data = pd.concat(frames, ignore_index=True).sort_values('date', kind='stable').reset_index(drop=True)
    log(f'{len(data)} matches, {data["league"].nunique()} leagues, '
        f'{data["date"].min().date()} .. {data["date"].max().date()}')
    return data
