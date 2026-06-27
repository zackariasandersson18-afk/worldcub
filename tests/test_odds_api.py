from unittest.mock import MagicMock, patch

import pytest

from wc26bot.odds_api import OddsApiError, fetch_world_cup_odds, find_match_odds

SAMPLE_RESPONSE = [
    {
        "home_team": "Argentina",
        "away_team": "France",
        "commence_time": "2026-06-15T18:00:00Z",
        "bookmakers": [
            {
                "title": "Pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Argentina", "price": 2.1},
                            {"name": "Draw", "price": 3.4},
                            {"name": "France", "price": 3.2},
                        ],
                    }
                ],
            }
        ],
    }
]


def test_missing_api_key_raises():
    with pytest.raises(OddsApiError):
        fetch_world_cup_odds(api_key=None)


@patch.dict("os.environ", {}, clear=True)
@patch("wc26bot.odds_api.requests.get")
def test_fetch_world_cup_odds_parses_response(mock_get):
    mock_response = MagicMock()
    mock_response.json.return_value = SAMPLE_RESPONSE
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    odds = fetch_world_cup_odds(api_key="fake-key")
    assert len(odds) == 1
    match = odds[0]
    assert match.home_team == "Argentina"
    assert match.away_team == "France"
    assert match.home_odds == 2.1
    assert match.draw_odds == 3.4
    assert match.away_odds == 3.2
    assert match.bookmaker == "Pinnacle"


@patch("wc26bot.odds_api.requests.get")
def test_find_match_odds_matches_case_insensitive(mock_get):
    mock_response = MagicMock()
    mock_response.json.return_value = SAMPLE_RESPONSE
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    match = find_match_odds("argentina", "france", api_key="fake-key")
    assert match is not None
    assert match.home_odds == 2.1


@patch("wc26bot.odds_api.requests.get")
def test_find_match_odds_returns_none_when_not_found(mock_get):
    mock_response = MagicMock()
    mock_response.json.return_value = SAMPLE_RESPONSE
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    match = find_match_odds("Brazil", "Germany", api_key="fake-key")
    assert match is None
