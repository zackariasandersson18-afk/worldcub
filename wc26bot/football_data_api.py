"""Klient mot football-data.org for riktig matchhistorik, anvand for att
rakna fram attack/defense-styrka (samma TeamStrength-modell som model.py
anvander) istallet for de manuellt ifyllda exempelvardena.

Kraver en API-nyckel: satt miljovariabeln FOOTBALL_DATA_API_KEY, eller
skicka in api_key direkt. Gratis tier raknar (begransat antal anrop/minut).
"""
from __future__ import annotations

import os

import requests

from wc26bot.model import TeamStrength

BASE_URL = "https://api.football-data.org/v4"


class FootballDataApiError(RuntimeError):
    pass


def _get_api_key(api_key: str | None) -> str:
    key = api_key or os.environ.get("FOOTBALL_DATA_API_KEY")
    if not key:
        raise FootballDataApiError(
            "Ingen API-nyckel hittades. Satt miljovariabeln FOOTBALL_DATA_API_KEY "
            "eller skicka in api_key=... (registrera dig pa football-data.org)."
        )
    return key


def _headers(api_key: str | None) -> dict[str, str]:
    return {"X-Auth-Token": _get_api_key(api_key)}


def fetch_recent_matches(
    team_id: int,
    api_key: str | None = None,
    limit: int = 10,
    timeout: float = 10.0,
) -> list[dict]:
    """Hamtar de senaste avslutade matcherna for ett lag (FINISHED status)."""
    url = f"{BASE_URL}/teams/{team_id}/matches"
    params = {"status": "FINISHED", "limit": limit}
    try:
        response = requests.get(
            url, headers=_headers(api_key), params=params, timeout=timeout
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise FootballDataApiError(f"Anrop till football-data.org misslyckades: {exc}") from exc
    return response.json().get("matches", [])


def team_strength_from_matches(
    team_id: int,
    team_name: str,
    api_key: str | None = None,
    limit: int = 10,
    league_avg_goals: float = 1.35,
) -> TeamStrength:
    """Raknar fram attack/defense-index fran lagets senaste matcher.

    attack = snitt gjorda mal / ligagenomsnitt
    defense = snitt insluppna mal / ligagenomsnitt (lagre = battre forsvar)
    """
    matches = fetch_recent_matches(team_id, api_key=api_key, limit=limit)
    if not matches:
        raise FootballDataApiError(f"Hittade inga avslutade matcher for lag-id {team_id}")

    goals_for = goals_against = 0
    for match in matches:
        home = match["homeTeam"]["id"] == team_id
        score = match["score"]["fullTime"]
        home_goals = score.get("home") or 0
        away_goals = score.get("away") or 0
        if home:
            goals_for += home_goals
            goals_against += away_goals
        else:
            goals_for += away_goals
            goals_against += home_goals

    n = len(matches)
    attack = (goals_for / n) / league_avg_goals
    defense = (goals_against / n) / league_avg_goals
    return TeamStrength(team_name, attack=round(attack, 2), defense=round(defense, 2))
