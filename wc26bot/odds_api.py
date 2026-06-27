"""Klient mot The Odds API (https://the-odds-api.com) for riktiga 1X2-odds.

Kraver en API-nyckel: satt miljovariabeln ODDS_API_KEY, eller skicka in
api_key direkt. Gratis tier racker for testning (begransat antal anrop/manad).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import requests

BASE_URL = "https://api.the-odds-api.com/v4"


class OddsApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class MatchOdds:
    home_team: str
    away_team: str
    home_odds: float
    draw_odds: float
    away_odds: float
    bookmaker: str
    commence_time: str


def _get_api_key(api_key: str | None) -> str:
    key = api_key or os.environ.get("ODDS_API_KEY")
    if not key:
        raise OddsApiError(
            "Ingen API-nyckel hittades. Satt miljovariabeln ODDS_API_KEY "
            "eller skicka in api_key=... (registrera dig pa the-odds-api.com)."
        )
    return key


def fetch_world_cup_odds(
    api_key: str | None = None,
    sport_key: str = "soccer_fifa_world_cup",
    regions: str = "eu",
    markets: str = "h2h",
    timeout: float = 10.0,
) -> list[MatchOdds]:
    """Hamtar kommande VM-matcher med 1X2-odds (h2h-market) fran forsta tillgangliga bookmaker."""
    key = _get_api_key(api_key)
    url = f"{BASE_URL}/sports/{sport_key}/odds"
    params = {
        "apiKey": key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "decimal",
    }
    try:
        response = requests.get(url, params=params, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise OddsApiError(f"Anrop till The Odds API misslyckades: {exc}") from exc

    results: list[MatchOdds] = []
    for event in response.json():
        bookmakers = event.get("bookmakers") or []
        if not bookmakers:
            continue
        bookmaker = bookmakers[0]
        h2h = next((m for m in bookmaker["markets"] if m["key"] == "h2h"), None)
        if h2h is None:
            continue

        home_team = event["home_team"]
        away_team = event["away_team"]
        prices = {o["name"]: o["price"] for o in h2h["outcomes"]}
        if home_team not in prices or away_team not in prices:
            continue
        draw_price = prices.get("Draw")
        if draw_price is None:
            continue

        results.append(
            MatchOdds(
                home_team=home_team,
                away_team=away_team,
                home_odds=prices[home_team],
                draw_odds=draw_price,
                away_odds=prices[away_team],
                bookmaker=bookmaker["title"],
                commence_time=event["commence_time"],
            )
        )
    return results


def find_match_odds(
    home_team: str, away_team: str, api_key: str | None = None, **kwargs
) -> MatchOdds | None:
    """Sok upp odds for en specifik match (case-insensitiv delstrangsmatchning)."""
    odds = fetch_world_cup_odds(api_key=api_key, **kwargs)
    home_l, away_l = home_team.lower(), away_team.lower()
    for m in odds:
        if home_l in m.home_team.lower() and away_l in m.away_team.lower():
            return m
    return None
