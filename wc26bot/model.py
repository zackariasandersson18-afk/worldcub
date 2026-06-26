"""Poisson-baserad modell for att skatta malsannolikheter och matchutfall."""
from __future__ import annotations

from dataclasses import dataclass
from math import exp, factorial


@dataclass(frozen=True)
class TeamStrength:
    """Anfalls- och forsvarsstyrka relativt ligagenomsnittet (1.0 = genomsnitt)."""

    name: str
    attack: float
    defense: float


@dataclass(frozen=True)
class MatchProbabilities:
    home_win: float
    draw: float
    away_win: float
    btts_yes: float
    over_2_5: float
    under_2_5: float
    home_goals_exp: float
    away_goals_exp: float


def poisson_pmf(k: int, lam: float) -> float:
    return exp(-lam) * lam**k / factorial(k)


def expected_goals(
    home: TeamStrength,
    away: TeamStrength,
    league_avg_home_goals: float = 1.5,
    league_avg_away_goals: float = 1.2,
) -> tuple[float, float]:
    """Skattar forvantat antal mal for varje lag enligt en Dixon-Coles-liknande ansats."""
    home_exp = home.attack * away.defense * league_avg_home_goals
    away_exp = away.attack * home.defense * league_avg_away_goals
    return home_exp, away_exp


def match_probabilities(
    home: TeamStrength,
    away: TeamStrength,
    max_goals: int = 10,
    league_avg_home_goals: float = 1.5,
    league_avg_away_goals: float = 1.2,
) -> MatchProbabilities:
    home_exp, away_exp = expected_goals(
        home, away, league_avg_home_goals, league_avg_away_goals
    )

    home_win = draw = away_win = btts_yes = over_2_5 = 0.0

    for h in range(max_goals + 1):
        p_h = poisson_pmf(h, home_exp)
        for a in range(max_goals + 1):
            p = p_h * poisson_pmf(a, away_exp)
            if h > a:
                home_win += p
            elif h == a:
                draw += p
            else:
                away_win += p
            if h > 0 and a > 0:
                btts_yes += p
            if h + a > 2.5:
                over_2_5 += p

    return MatchProbabilities(
        home_win=home_win,
        draw=draw,
        away_win=away_win,
        btts_yes=btts_yes,
        over_2_5=over_2_5,
        under_2_5=1 - over_2_5,
        home_goals_exp=home_exp,
        away_goals_exp=away_exp,
    )
