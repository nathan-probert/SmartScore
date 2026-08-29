from datetime import datetime
from unittest.mock import patch

import pytz
from smartscore_info_client.api.nhle import NHLClient
from smartscore_info_client.models.player import PlayerInfo, PlayerStats
from smartscore_info_client.models.team import GameTeam

from mock_nhl_client import MockNHLClient
from service import (
    backfill_dates,
    choose_picks,
    get_date,
    get_nhl_client,
    get_players_from_team,
    merge_injury_data,
    merge_players_and_teams,
    send_emails,
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


@patch("service.is_feature_enabled", return_value=False)
def test_get_nhl_client_returns_real_client_when_flag_off(mock_flag):
    """With the mock flag off, get_nhl_client returns a live NHLClient."""
    client = get_nhl_client()
    assert isinstance(client, NHLClient)
    assert not isinstance(client, MockNHLClient)


@patch("service.is_feature_enabled", return_value=True)
def test_get_nhl_client_returns_mock_client_when_flag_on(mock_flag):
    """With the mock flag on, get_nhl_client returns a MockNHLClient."""
    client = get_nhl_client()
    assert isinstance(client, MockNHLClient)


def test_merge_players_and_teams_merges_player_and_team_data():
    """Test that merge_players_and_teams correctly combines player and team data."""
    teams = [
        {
            "team_name": "Team A",
            "team_abbr": "TA",
            "season": "20242025",
            "team_id": 10,
            "opponent_id": 11,
            "home": True,
            "tgpg": 3.0,
            "otga": 2.8,
            "otshga": 0.4,
            "players": [
                {
                    "name": "Player One",
                    "id": 1,
                    "team_id": 10,
                    "gpg": 0.5,
                    "hgpg": 0.4,
                    "five_gpg": 0.45,
                    "hppg": 0.1,
                },
            ],
        },
        {
            "team_name": "Team B",
            "team_abbr": "TB",
            "season": "20242025",
            "team_id": 20,
            "opponent_id": 10,
            "home": False,
            "tgpg": 2.7,
            "otga": 3.1,
            "otshga": 0.5,
            "players": [
                {
                    "name": "Player Two",
                    "id": 2,
                    "team_id": 20,
                    "gpg": 0.6,
                    "hgpg": 0.5,
                    "five_gpg": 0.5,
                    "hppg": 0.2,
                },
            ],
        },
    ]

    result = merge_players_and_teams(teams)
    assert len(result) == 2
    assert result[0]["name"] == "Player One"
    assert result[0]["id"] == 1
    assert result[0]["team_name"] == "Team A"
    assert result[0]["home"] is True
    assert result[0]["tgpg"] == 3.0
    assert result[0]["otga"] == 2.8
    assert result[0]["otshga"] == 0.4
    assert result[1]["name"] == "Player Two"
    assert result[1]["id"] == 2
    assert result[1]["team_name"] == "Team B"
    assert result[1]["home"] is False


def test_merge_players_and_teams_excludes_fields():
    """Test that certain fields are excluded from the result."""
    teams = [
        {
            "team_name": "Team A",
            "team_abbr": "TA",
            "season": "20242025",
            "team_id": 10,
            "opponent_id": 11,
            "home": True,
            "players": [
                {"name": "Player One", "id": 1, "team_id": 10, "odds": None, "stat": 0.8},
            ],
        },
    ]

    result = merge_players_and_teams(teams)
    for entry in result:
        assert "team_id" not in entry
        assert "opponent_id" not in entry
        assert "season" not in entry
        assert "team_abbr" not in entry
        assert "odds" not in entry
        assert "stat" not in entry


def test_merge_players_and_teams_empty():
    """Test merge_players_and_teams with empty lists."""
    result = merge_players_and_teams([])

    assert result == []


@patch("service.get_nhl_client")
def test_get_players_from_team_builds_player_info(mock_client):
    """Test that get_players_from_team fetches roster and stats explicitly."""
    mock_client.return_value.get_roster.return_value = {
        "forwards": [
            {"id": 1, "firstName": {"default": "John"}, "lastName": {"default": "Doe"}},
            {"id": 2, "firstName": {"default": "Jane"}, "lastName": {"default": "Roe"}},
        ],
        "defensemen": [],
    }
    mock_client.return_value.get_player_stats.side_effect = lambda player_id: PlayerStats(
        gpg=float(player_id), hgpg=0.5, five_gpg=0.4, hppg=0.3
    )

    team = GameTeam(
        team_name="Team A",
        team_abbr="TA",
        season="20242025",
        team_id=10,
        opponent_id=11,
        home=True,
    )

    players = get_players_from_team(team)

    assert len(players) == 2
    assert all(isinstance(player, PlayerInfo) for player in players)
    assert players[0].name == "John Doe"
    assert players[0].id == 1
    assert players[0].team_id == 10
    assert players[0].gpg == 1.0
    assert players[1].name == "Jane Roe"
    assert players[1].id == 2
    mock_client.return_value.get_roster.assert_called_once_with("TA")
    assert mock_client.return_value.get_player_stats.call_count == 2


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


@patch("service.send_email")
@patch("service.is_feature_enabled", return_value=False)
def test_send_emails_respects_disabled_feature_flag(mock_feature_enabled, mock_send_email):
    """Test send_emails skips work when send_emails feature flag is disabled."""
    users = [{"email": "test@example.com", "display_name": "Tester"}]
    picks = [{"name": "Player 1", "stat": 0.9, "tims": 1}]

    send_emails(users, picks)

    mock_feature_enabled.assert_called_once_with("send_emails")
    mock_send_email.assert_not_called()


@patch("service.send_email")
@patch("service.get_date", return_value="2026-04-16")
@patch("service.is_feature_enabled", return_value=True)
def test_send_emails_sends_when_feature_flag_enabled(mock_feature_enabled, mock_get_date, mock_send_email):
    """Test send_emails dispatches emails when send_emails feature flag is enabled."""
    users = [{"email": "test@example.com", "display_name": "Tester"}]
    picks = [{"name": "Player 1", "stat": 0.9, "tims": 1}]

    send_emails(users, picks)

    mock_feature_enabled.assert_called_once_with("send_emails")
    mock_get_date.assert_called_once()
    mock_send_email.assert_called_once_with("test@example.com", picks, "Tester", "2026-04-16")


@patch("service.datetime")
@patch("service.invoke_lambda")
@patch("service.get_nhl_client")
def test_backfill_dates_fetches_score_via_client_and_builds_scorers(mock_client, mock_invoke, mock_datetime):
    """Test backfill_dates drives the NHL client's get_score and reports scorers.

    Exercises the refactored ``_get_score`` path (via ``get_nhl_client()``) that
    the backfill flow relies on, plus scorer extraction from the score payload.
    """
    # Freeze "today" so yesterday resolves to 2025-06-11 (the backfill date).
    mock_now = datetime(2025, 6, 12, 12, 0, 0, tzinfo=pytz.timezone("America/Toronto"))
    mock_datetime.datetime.now.return_value = mock_now
    import datetime as real_datetime

    mock_datetime.timedelta = real_datetime.timedelta

    # GET_DATES_NO_SCORED returns a single date to backfill; POST_BACKFILL returns 200.
    mock_invoke.side_effect = [
        {"body": {"dates": '["2025-06-11"]'}},  # Api-{ENV} GET_DATES_NO_SCORED
        {"statusCode": 200},  # LAMBDA_API_NAME POST_BACKFILL
    ]

    client = mock_client.return_value
    client.get_score.return_value = {
        "games": [
            {
                "gameScheduleState": "OK",
                "gameOutcome": {"lastPeriod": 3},
                "goals": [{"playerId": 100}, {"playerId": 100}, {"playerId": 200}],
            }
        ]
    }

    backfill_dates()

    # The backfill path must fetch the score through the (mock-selecting) client.
    client.get_score.assert_called_once_with("2025-06-11")

    # POST_BACKFILL receives the deduplicated scorer ids per date.
    backfill_calls = [c for c in mock_invoke.call_args_list if c.args[1]["method"] == "POST_BACKFILL"]
    assert len(backfill_calls) == 1
    reported = backfill_calls[0].args[1]["data"]
    # Scorer ids come from a set, so compare ignoring order.
    assert set(reported["2025-06-11"]) == {100, 200}
