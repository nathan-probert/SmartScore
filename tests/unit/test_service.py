from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import pytz

import sys
import types
import pytest
from unittest.mock import patch, MagicMock

from service import (
    choose_picks,
    get_date,
    merge_injury_data,
    separate_players,
)


@patch("service.datetime")
def test_get_date_default(mock_datetime):
    """Test get_date with default parameters."""
    mock_now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=pytz.timezone("America/Toronto"))
    mock_datetime.datetime.now.return_value = mock_now

    result = get_date()

    assert result == "2024-01-15"
    mock_datetime.datetime.now.assert_called_once_with(pytz.timezone("America/Toronto"))


@patch("service.datetime")
def test_get_date_with_hour(mock_datetime):
    """Test get_date with hour=True."""
    mock_now = datetime(2024, 1, 15, 14, 30, 0, tzinfo=pytz.timezone("America/Toronto"))
    mock_datetime.datetime.now.return_value = mock_now

    result = get_date(hour=True)

    assert result == "2024-01-15T14:30:00"



@patch("service.datetime")
def test_get_date_add_days(mock_datetime):
    """Test get_date with add_days parameter."""
    mock_now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=pytz.timezone("America/Toronto"))
    mock_datetime.datetime.now.return_value = mock_now
    # Use real timedelta
    import datetime as real_datetime
    mock_datetime.timedelta = real_datetime.timedelta

    result = get_date(add_days=5)

    assert result == "2024-01-20"



@patch("service.datetime")
def test_get_date_subtract_days(mock_datetime):
    """Test get_date with subtract_days parameter."""
    mock_now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=pytz.timezone("America/Toronto"))
    mock_datetime.datetime.now.return_value = mock_now
    import datetime as real_datetime
    mock_datetime.timedelta = real_datetime.timedelta

    result = get_date(subtract_days=3)

    assert result == "2024-01-12"



@patch("service.PLAYER_INFO_SCHEMA")
@patch("service.TEAM_INFO_SCHEMA")
def test_separate_players_basic(mock_team_schema, mock_player_schema):
    """Test that separate_players correctly combines player and team data."""
    # Mock dump to return expected dicts
    mock_player_schema.dump.side_effect = lambda p: {"name": p.name, "id": p.id}
    mock_team_schema.dump.side_effect = lambda t: {
        "team_name": t.team_name,
        "team_id": t.team_id,
        "home": t.home,
        "tgpg": getattr(t, "tgpg", 0.0),
    }
    class Player:
        def __init__(self, name, id, team_id):
            self.name = name
            self.id = id
            self.team_id = team_id
    players = [
        Player("Player One", 1, 10),
        Player("Player Two", 2, 20),
    ]
    class Team:
        def __init__(self, team_name, team_id, home, tgpg):
            self.team_name = team_name
            self.team_id = team_id
            self.home = home
            self.tgpg = tgpg
    teams = [
        Team("Team A", 10, True, 3.0),
        Team("Team B", 20, False, 2.8),
    ]
    result = separate_players(players, teams)
    assert len(result) == 2
    assert result[0]["name"] == "Player One"
    assert result[0]["id"] == 1
    assert result[0]["team_name"] == "Team A"
    assert result[0]["home"] is True
    assert result[0]["tgpg"] == 3.0
    assert result[1]["name"] == "Player Two"
    assert result[1]["id"] == 2
    assert result[1]["team_name"] == "Team B"
    assert result[1]["home"] is False
    assert result[1]["tgpg"] == 2.8



@patch("service.PLAYER_INFO_SCHEMA")
@patch("service.TEAM_INFO_SCHEMA")
def test_separate_players_excludes_fields(mock_team_schema, mock_player_schema):
    """Test that certain fields are excluded from the result."""
    mock_player_schema.dump.side_effect = lambda p: {"name": p.name, "id": p.id}
    mock_team_schema.dump.side_effect = lambda t: {
        "team_name": t.team_name,
        "team_id": t.team_id,
        "home": t.home,
    }
    class Player:
        def __init__(self, name, id, team_id):
            self.name = name
            self.id = id
            self.team_id = team_id
    players = [
        Player("Player One", 1, 10),
    ]
    class Team:
        def __init__(self, team_name, team_id, home):
            self.team_name = team_name
            self.team_id = team_id
            self.home = home
    teams = [
        Team("Team A", 10, True),
    ]
    result = separate_players(players, teams)
    for entry in result:
        assert "team_id" not in entry
        assert "opponent_id" not in entry
        assert "season" not in entry
        assert "team_abbr" not in entry
        assert "odds" not in entry
        assert "stat" not in entry


def test_separate_players_empty_lists():
    """Test separate_players with empty lists."""
    result = separate_players([], [])

    assert result == []


def test_choose_picks_basic():
    """Test choose_picks selects top player from each tims group."""
    players = [
        {"name": "Player 1", "stat": 0.8, "tims": 1},
        {"name": "Player 2", "stat": 0.9, "tims": 1},
        {"name": "Player 3", "stat": 0.7, "tims": 2},
        {"name": "Player 4", "stat": 0.85, "tims": 2},
        {"name": "Player 5", "stat": 0.6, "tims": 3},
        {"name": "Player 6", "stat": 0.75, "tims": 3},
    ]

    result = choose_picks(players)

    assert len(result) == 3
    assert result[0]["name"] == "Player 2"  # Highest in tims 1
    assert result[0]["stat"] == 0.9
    assert result[1]["name"] == "Player 4"  # Highest in tims 2
    assert result[1]["stat"] == 0.85
    assert result[2]["name"] == "Player 6"  # Highest in tims 3
    assert result[2]["stat"] == 0.75

    # Check that Scored field is added
    for player in result:
        assert player["Scored"] is None


def test_merge_injury_data_with_matches():
    """Test merging injury data with player data."""
    players = [
        {"name": "Auston Matthews", "team": "TOR", "stat": 0.8},
        {"name": "Connor McDavid", "team": "EDM", "stat": 0.9},
        {"name": "Nathan MacKinnon", "team": "COL", "stat": 0.85},
    ]

    injuries = [
        {"player": "Auston Matthews", "injury": "Upper Body", "status": "Day-to-Day"},
        {"player": "Connor McDavid", "injury": "Lower Body", "status": "Out"},
    ]

    result = merge_injury_data(players, injuries)

    assert len(result) == 3
    assert result[0]["injury_status"] == "INJURED"
    assert result[0]["injury_desc"] == "Day-to-Day"
    assert result[1]["injury_status"] == "INJURED"
    assert result[1]["injury_desc"] == "Out"
    assert result[2]["injury_status"] == "HEALTHY"
    assert result[2]["injury_desc"] == ""


def test_merge_injury_data_case_insensitive():
    """Test that name matching is case-insensitive."""
    players = [
        {"name": "AUSTON MATTHEWS", "stat": 0.8},
    ]

    injuries = [
        {"player": "auston matthews", "injury": "Upper Body", "status": "Day-to-Day"},
    ]

    result = merge_injury_data(players, injuries)

    assert result[0]["injury_status"] == "INJURED"


def test_merge_injury_data_no_matches():
    """Test merging when no players match injury data."""
    players = [
        {"name": "Player One", "team": "TOR", "stat": 0.8},
        {"name": "Player Two", "team": "EDM", "stat": 0.9},
    ]

    injuries = [
        {"player": "Different Player", "injury": "Ankle", "status": "Out"},
    ]

    result = merge_injury_data(players, injuries)

    assert len(result) == 2
    for player in result:
        assert player["injury_status"] == "HEALTHY"
        assert player["injury_desc"] == ""


def test_merge_injury_data_empty_injuries():
    """Test merging with empty injury list."""
    players = [
        {"name": "Player One", "stat": 0.8},
    ]

    result = merge_injury_data(players, [])

    assert len(result) == 1
    assert result[0]["injury_status"] == "HEALTHY"


def test_merge_injury_data_empty_players():
    """Test merging with empty player list."""
    injuries = [
        {"player": "Player One", "injury": "Ankle", "status": "Out"},
    ]

    result = merge_injury_data([], injuries)

    assert result == []
