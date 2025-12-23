from unittest.mock import patch

import pytest
from smartscore_info_client.schemas.player_info import PlayerInfo
from smartscore_info_client.schemas.team_info import TeamInfo

from event_handler import handle_get_players_from_team


@pytest.fixture
def mock_get_players_from_team():
    with patch("event_handler.get_players_from_team") as mock:
        yield mock


@pytest.fixture
def sample_team_event():
    return {
        "team_name": "Florida Panthers",
        "team_abbr": "FLA",
        "season": "20242025",
        "team_id": 13,
        "opponent_id": 14,
        "home": True,
        "tgpg": 3.2,
        "otga": 2.86585,
        "otshga": 0.5487804878048781,
    }


@pytest.fixture
def sample_players():
    return [
        PlayerInfo(
            name="Aleksander Barkov",
            id=8477493,
            team_id=13,
        ),
        PlayerInfo(
            name="Sam Bennett",
            id=8477935,
            team_id=13,
        ),
        PlayerInfo(
            name="Matthew Tkachuk",
            id=8479314,
            team_id=13,
        ),
    ]


def test_handle_get_players_from_team_returns_complete_structure(
    mock_get_players_from_team, sample_team_event, sample_players
):
    """Test that the handler returns the complete team structure with players."""
    mock_get_players_from_team.return_value = sample_players

    result = handle_get_players_from_team(sample_team_event, {})

    # Verify the service was called with correct TeamInfo
    mock_get_players_from_team.assert_called_once()
    called_team = mock_get_players_from_team.call_args[0][0]
    assert isinstance(called_team, TeamInfo)
    assert called_team.team_name == "Florida Panthers"
    assert called_team.team_id == 13

    # Verify the result contains all team information
    assert result["team_name"] == "Florida Panthers"
    assert result["team_abbr"] == "FLA"
    assert result["season"] == "20242025"
    assert result["team_id"] == 13
    assert result["opponent_id"] == 14
    assert result["home"] is True

    # Verify players are included in the result
    assert "players" in result
    assert isinstance(result["players"], list)
    assert len(result["players"]) == 3

    # Verify player data structure
    player_names = [p["name"] for p in result["players"]]
    assert "Aleksander Barkov" in player_names
    assert "Sam Bennett" in player_names
    assert "Matthew Tkachuk" in player_names

    # Verify player IDs are preserved
    player_ids = [p["id"] for p in result["players"]]
    assert 8477493 in player_ids
    assert 8477935 in player_ids
    assert 8479314 in player_ids


def test_handle_get_players_from_team_with_empty_players(mock_get_players_from_team, sample_team_event):
    """Test that the handler works correctly when no players are returned."""
    mock_get_players_from_team.return_value = []

    result = handle_get_players_from_team(sample_team_event, {})

    # Verify all team information is still present
    assert result["team_name"] == "Florida Panthers"
    assert result["team_abbr"] == "FLA"
    assert result["season"] == "20242025"
    assert result["team_id"] == 13
    assert result["opponent_id"] == 14
    assert result["home"] is True

    # Verify players list is empty
    assert result["players"] == []


def test_handle_get_players_from_team_away_team(mock_get_players_from_team, sample_players):
    """Test that the handler works correctly for away teams."""
    away_team_event = {
        "team_name": "Tampa Bay Lightning",
        "team_abbr": "TBL",
        "season": "20242025",
        "team_id": 14,
        "opponent_id": 13,
        "home": False,
        "tgpg": 3.0,
        "otga": 2.5,
        "otshga": 0.6,
    }

    # Update sample players to have different team_id
    away_players = [
        PlayerInfo(name="Nikita Kucherov", id=8476453, team_id=14),
        PlayerInfo(name="Brayden Point", id=8478010, team_id=14),
    ]
    mock_get_players_from_team.return_value = away_players

    result = handle_get_players_from_team(away_team_event, {})

    # Verify team information
    assert result["team_name"] == "Tampa Bay Lightning"
    assert result["team_abbr"] == "TBL"
    assert result["team_id"] == 14
    assert result["opponent_id"] == 13
    assert result["home"] is False

    # Verify players
    assert len(result["players"]) == 2
    player_names = [p["name"] for p in result["players"]]
    assert "Nikita Kucherov" in player_names
    assert "Brayden Point" in player_names


def test_handle_get_players_from_team_preserves_all_event_fields(mock_get_players_from_team, sample_players):
    """Test that all fields from the event are preserved in the output."""
    event_with_extra_fields = {
        "team_name": "Boston Bruins",
        "team_abbr": "BOS",
        "season": "20242025",
        "team_id": 6,
        "opponent_id": 7,
        "home": True,
        "tgpg": 3.5,
        "otga": 2.8,
        "otshga": 0.55,
    }

    mock_get_players_from_team.return_value = sample_players

    result = handle_get_players_from_team(event_with_extra_fields, {})

    # Verify all expected fields are present
    expected_keys = {"team_name", "team_abbr", "season", "team_id", "opponent_id", "home", "players"}
    assert set(result.keys()) == expected_keys

    # Verify values
    assert result["team_name"] == "Boston Bruins"
    assert result["team_abbr"] == "BOS"
    assert result["season"] == "20242025"
    assert result["team_id"] == 6
    assert result["opponent_id"] == 7
    assert result["home"] is True
