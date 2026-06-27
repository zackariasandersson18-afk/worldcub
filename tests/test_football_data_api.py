from unittest.mock import MagicMock, patch

import pytest

from wc26bot.football_data_api import (
    FootballDataApiError,
    team_strength_from_matches,
)

SAMPLE_MATCHES = {
    "matches": [
        {
            "homeTeam": {"id": 1},
            "awayTeam": {"id": 2},
            "score": {"fullTime": {"home": 2, "away": 1}},
        },
        {
            "homeTeam": {"id": 2},
            "awayTeam": {"id": 1},
            "score": {"fullTime": {"home": 0, "away": 3}},
        },
    ]
}


def test_missing_api_key_raises():
    with pytest.raises(FootballDataApiError):
        team_strength_from_matches(1, "Test", api_key=None)


@patch("wc26bot.football_data_api.requests.get")
def test_team_strength_from_matches_computes_attack_defense(mock_get):
    mock_response = MagicMock()
    mock_response.json.return_value = SAMPLE_MATCHES
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    strength = team_strength_from_matches(1, "Argentina", api_key="fake-key")
    # team_id=1: gjorde 2 (home) + 3 (away) = 5 mal over 2 matcher -> 2.5/match
    # insluppna: 1 (home) + 0 (away) = 1 mal -> 0.5/match
    assert strength.attack == pytest.approx(2.5 / 1.35, rel=1e-3)
    assert strength.defense == pytest.approx(0.5 / 1.35, rel=1e-3)


@patch("wc26bot.football_data_api.requests.get")
def test_no_matches_raises(mock_get):
    mock_response = MagicMock()
    mock_response.json.return_value = {"matches": []}
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    with pytest.raises(FootballDataApiError):
        team_strength_from_matches(1, "Test", api_key="fake-key")
