from unittest.mock import patch

from event_handler import (
    handle_check_completed,
    handle_get_injuries,
    handle_make_predictions,
    handle_parse_teams,
    handle_publish_db,
)


@patch("event_handler.check_db_for_date")
def test_handle_check_completed_first_run(mock_check_db):
    """Test when no date exists in database (first run)."""
    mock_check_db.return_value = None

    result = handle_check_completed({}, {})

    assert result == {"statusCode": 200, "status": "first_run", "players": None}
    mock_check_db.assert_called_once()


@patch("event_handler.check_db_for_date")
def test_handle_check_completed_normal_run(mock_check_db):
    """Test when date exists in database (normal run)."""
    mock_entries = [
        {"player_id": 1, "name": "Player 1", "date": "2024-01-15"},
        {"player_id": 2, "name": "Player 2", "date": "2024-01-15"},
    ]
    mock_check_db.return_value = mock_entries

    result = handle_check_completed({}, {})

    assert result["statusCode"] == 200
    assert result["status"] == "normal_run"
    assert result["players"] == mock_entries
    mock_check_db.assert_called_once()


@patch("event_handler.check_db_for_date")
def test_handle_check_completed_last_game(mock_check_db):
    """Test when last_game flag is set."""
    result = handle_check_completed({"last_game": True}, {})

    assert result == {"statusCode": 200, "status": "last_run", "players": None}
    mock_check_db.assert_not_called()


@patch("event_handler.publish_public_db")
def test_handle_publish_db_with_players(mock_publish):
    """Test publishing database with player data."""
    players = [
        {"name": "Player 1", "stat": 0.8},
        {"name": "Player 2", "stat": 0.9},
    ]

    event = {"players": players}
    result = handle_publish_db(event, {})

    assert result == {"statusCode": 200}
    mock_publish.assert_called_once_with(players)


@patch("event_handler.publish_public_db")
def test_handle_publish_db_empty_players(mock_publish):
    """Test publishing database with empty player list."""
    event = {"players": []}
    result = handle_publish_db(event, {})

    assert result == {"statusCode": 200}
    mock_publish.assert_called_once_with([])


@patch("event_handler.publish_public_db")
def test_handle_publish_db_no_players_key(mock_publish):
    """Test publishing database when players key is missing."""
    event = {}
    result = handle_publish_db(event, {})

    assert result == {"statusCode": 200}
    mock_publish.assert_called_once_with([])


def test_handle_parse_teams_empty_event():
    """Test handling empty event."""
    result = handle_parse_teams([], {})

    assert result == []


@patch("event_handler.separate_players")
@patch("event_handler.PlayerInfo")
@patch("event_handler.TeamInfo")
def test_handle_parse_teams_with_data(mock_teaminfo, mock_playerinfo, mock_separate):
    """Test parsing teams with player and team data."""
    mock_separate.return_value = [
        {"name": "Player 1", "team_name": "Team A", "stat": 0.8},
        {"name": "Player 2", "team_name": "Team B", "stat": 0.9},
    ]

    # Mock PlayerInfo and TeamInfo to just return dicts
    mock_playerinfo.side_effect = lambda **kwargs: kwargs
    mock_teaminfo.side_effect = lambda **kwargs: kwargs

    event = [
        {
            "team_name": "Team A",
            "team_id": 1,
            "opponent_id": 2,
            "home": True,
            "team_abbr": "TA",
            "season": "20242025",
            "players": [{"name": "Player 1", "id": 100, "team_id": 1}],
        },
        {
            "team_name": "Team B",
            "team_id": 2,
            "opponent_id": 1,
            "home": False,
            "team_abbr": "TB",
            "season": "20242025",
            "players": [{"name": "Player 2", "id": 200, "team_id": 2}],
        },
    ]

    result = handle_parse_teams(event, {})

    assert len(result) == 2
    mock_separate.assert_called_once()


@patch("event_handler.make_predictions_teams")
def test_handle_make_predictions(mock_predictions):
    """Test making predictions for players."""
    input_players = [
        {"name": "Player 1", "gpg": 0.5},
        {"name": "Player 2", "gpg": 0.7},
    ]

    output_players = [
        {"name": "Player 1", "gpg": 0.5, "stat": 0.6},
        {"name": "Player 2", "gpg": 0.7, "stat": 0.8},
    ]

    mock_predictions.return_value = output_players

    event = {"players": input_players}
    result = handle_make_predictions(event, {})

    assert result == {"statusCode": 200, "players": output_players}
    mock_predictions.assert_called_once_with(input_players)


@patch("event_handler.merge_injury_data")
@patch("event_handler.get_injury_data")
def test_handle_get_injuries_with_data(mock_get_injuries, mock_merge):
    """Test handling injury data retrieval and merging."""
    players = [
        {"name": "Player 1", "stat": 0.8},
        {"name": "Player 2", "stat": 0.9},
    ]

    injuries = [
        {"player": "Player 1", "injury": "Upper Body", "status": "Day-to-Day"},
    ]

    merged_players = [
        {"name": "Player 1", "stat": 0.8, "injury_status": "INJURED", "injury_desc": "Day-to-Day"},
        {"name": "Player 2", "stat": 0.9, "injury_status": "HEALTHY", "injury_desc": ""},
    ]

    mock_get_injuries.return_value = injuries
    mock_merge.return_value = merged_players

    event = {"players": players}
    result = handle_get_injuries(event, {})

    assert result == {"statusCode": 200, "players": merged_players}
    mock_get_injuries.assert_called_once()
    mock_merge.assert_called_once_with(players, injuries)


@patch("event_handler.merge_injury_data")
@patch("event_handler.get_injury_data")
def test_handle_get_injuries_empty_players(mock_get_injuries, mock_merge):
    """Test handling injury data with empty player list."""
    mock_get_injuries.return_value = []
    mock_merge.return_value = []

    event = {}  # No players key
    result = handle_get_injuries(event, {})

    assert result == {"statusCode": 200, "players": []}
    mock_get_injuries.assert_called_once()
    mock_merge.assert_called_once_with([], [])


@patch("event_handler.merge_injury_data")
@patch("event_handler.get_injury_data")
def test_handle_get_injuries_no_injuries_found(mock_get_injuries, mock_merge):
    """Test when no injuries are found."""
    players = [
        {"name": "Player 1", "stat": 0.8},
    ]

    healthy_players = [
        {"name": "Player 1", "stat": 0.8, "injury_status": "HEALTHY", "injury_desc": ""},
    ]

    mock_get_injuries.return_value = []
    mock_merge.return_value = healthy_players

    event = {"players": players}
    result = handle_get_injuries(event, {})

    assert result == {"statusCode": 200, "players": healthy_players}
    mock_merge.assert_called_once_with(players, [])
