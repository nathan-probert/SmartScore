from unittest.mock import patch

from event_handler import (
    handle_check_completed,
    handle_get_goalies,
    handle_get_injuries,
    handle_make_predictions,
    handle_parse_teams,
    handle_publish_db,
    handle_save_players,
)


@patch("event_handler.upload_players")
def test_handle_save_players_uploads_with_date(mock_upload):
    """Players are uploaded to the worker with the run's date."""
    players = [{"id": 1, "name": "Player 1", "tims": 1}]

    result = handle_save_players({"players": players, "date": "2026-04-15"}, {})

    assert result == {"statusCode": 200, "players": players}
    mock_upload.assert_called_once_with(players, date="2026-04-15")


@patch("event_handler.upload_players")
def test_handle_save_players_skips_upload_when_empty(mock_upload):
    """An empty player list must not hit the worker (it rejects empty arrays)."""
    result = handle_save_players({"players": [], "date": "2026-04-15"}, {})

    assert result == {"statusCode": 200, "players": []}
    mock_upload.assert_not_called()


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
    mock_entries = [
        {"player_id": 1, "name": "Player 1", "date": "2024-01-15"},
        {"player_id": 2, "name": "Player 2", "date": "2024-01-15"},
    ]
    mock_check_db.return_value = mock_entries

    result = handle_check_completed({"last_game": True}, {})

    assert result == {"statusCode": 200, "status": "last_run", "players": mock_entries}


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


@patch("event_handler.merge_players_and_teams")
def test_handle_parse_teams_with_data(mock_merge):
    """Test parsing teams with player and team data."""
    mock_merge.return_value = [
        {"name": "Player 1", "team_name": "Team A", "stat": 0.8},
        {"name": "Player 2", "team_name": "Team B", "stat": 0.9},
    ]

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
    mock_merge.assert_called_once_with(event)


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


@patch("event_handler.merge_goalie_data")
@patch("event_handler.enrich_starting_goalies")
@patch("event_handler.get_todays_schedule")
def test_handle_get_goalies_with_data(mock_schedule, mock_enrich, mock_merge):
    """Test handling starting goalie retrieval and merging."""
    players = [
        {"name": "Player 1", "stat": 0.8},
        {"name": "Player 2", "stat": 0.9},
    ]
    schedule = {"gameWeek": []}
    starters = [{"team_abbr": "CAR", "goalie_name": "Brandon Bussi"}]
    merged_players = [
        {"name": "Player 1", "opp_goalie_name": "Jacob Markstrom"},
        {"name": "Player 2", "opp_goalie_name": "Brandon Bussi"},
    ]

    mock_schedule.return_value = schedule
    mock_enrich.return_value = starters
    mock_merge.return_value = merged_players

    event = {"players": players}
    result = handle_get_goalies(event, {})

    assert result == {"statusCode": 200, "players": merged_players}
    mock_schedule.assert_called_once()
    mock_enrich.assert_called_once()
    mock_merge.assert_called_once_with(players, starters, schedule)


@patch("event_handler.merge_goalie_data")
@patch("event_handler.enrich_starting_goalies")
@patch("event_handler.get_todays_schedule")
def test_handle_get_goalies_empty_players(mock_schedule, mock_enrich, mock_merge):
    """Test handling goalie data with empty player list."""
    mock_schedule.return_value = {"gameWeek": []}
    mock_enrich.return_value = []
    mock_merge.return_value = []

    event = {}
    result = handle_get_goalies(event, {})

    assert result == {"statusCode": 200, "players": []}
    mock_merge.assert_called_once_with([], [], {"gameWeek": []})


@patch("event_handler.enrich_starting_goalies")
@patch("event_handler.get_todays_schedule", side_effect=Exception("boom"))
def test_handle_get_goalies_schedule_failure(mock_schedule, mock_enrich):
    """Test players pass through unchanged when the schedule fetch fails."""
    players = [{"name": "Player 1", "stat": 0.8}]

    result = handle_get_goalies({"players": players}, {})

    assert result == {"statusCode": 200, "players": players}
    mock_enrich.assert_not_called()
