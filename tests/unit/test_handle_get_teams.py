from unittest.mock import MagicMock, patch

import pytest
from smartscore_info_client.schemas.team_info import TeamInfo

from event_handler import handle_get_teams


@pytest.fixture
def mock_get_todays_schedule():
    with patch("event_handler.get_todays_schedule") as mock:
        yield mock


@patch("smartscore_info_client.schemas.team_info.requests.get")
@patch("service.schedule_run")
def test_handle_get_1_game(mock_schedule_run, mock_team_info_requests_get, mock_get_todays_schedule):
    TeamInfo._class_data_summary = None
    TeamInfo._class_data_penalties = None

    mock_nhl_api_response = MagicMock()
    mock_nhl_api_response.json.return_value = {"data": []}
    mock_team_info_requests_get.return_value = mock_nhl_api_response

    # Realistic mock_schedule_data that get_teams will process
    mock_schedule_data = {
        "gameWeek": [
            {
                "games": [
                    {
                        "startTimeUTC": "2025-06-11T23:00:00Z",
                        "season": "20242025",
                        "homeTeam": {
                            "id": 1,
                            "placeName": {"default": "Team Alpha"},
                            "abbrev": "TA",
                            "commonName": {"default": "Alpha"},
                        },
                        "awayTeam": {
                            "id": 2,
                            "placeName": {"default": "Team Bravo"},
                            "abbrev": "TB",
                            "commonName": {"default": "Bravo"},
                        },
                    }
                ]
            }
        ]
    }
    mock_get_todays_schedule.return_value = mock_schedule_data

    expected_teams_data = [
        {
            "team_name": "Team Alpha",
            "team_abbr": "TA",
            "season": "20242025",
            "team_id": 1,
            "opponent_id": 2,
            "home": True,
            "tgpg": 0.0,
            "otga": 0.0,
            "otshga": 0.0,
        },
        {
            "team_name": "Team Bravo",
            "team_abbr": "TB",
            "season": "20242025",
            "team_id": 2,
            "opponent_id": 1,
            "home": False,
            "tgpg": 0.0,
            "otga": 0.0,
            "otshga": 0.0,
        },
    ]
    expected_response = {"statusCode": 200, "teams": expected_teams_data}

    response = handle_get_teams({}, {})

    mock_get_todays_schedule.assert_called_once()
    mock_schedule_run.assert_called_once_with(set())
    assert response == expected_response


@patch("smartscore_info_client.schemas.team_info.requests.get")
@patch("service.schedule_run")
def test_handle_get_2_games(mock_schedule_run, mock_team_info_requests_get, mock_get_todays_schedule):
    TeamInfo._class_data_summary = None
    TeamInfo._class_data_penalties = None

    mock_nhl_api_response = MagicMock()
    mock_nhl_api_response.json.return_value = {"data": []}
    mock_team_info_requests_get.return_value = mock_nhl_api_response

    # Realistic mock_schedule_data that get_teams will process
    mock_schedule_data = {
        "gameWeek": [
            {
                "games": [
                    {
                        "startTimeUTC": "2025-06-11T23:00:00Z",
                        "season": "20242025",
                        "homeTeam": {
                            "id": 1,
                            "placeName": {"default": "Team Alpha"},
                            "abbrev": "TA",
                            "commonName": {"default": "Alpha"},
                        },
                        "awayTeam": {
                            "id": 2,
                            "placeName": {"default": "Team Bravo"},
                            "abbrev": "TB",
                            "commonName": {"default": "Bravo"},
                        },
                    },
                    {
                        "startTimeUTC": "2025-06-11T01:00:00Z",
                        "season": "20242025",
                        "homeTeam": {
                            "id": 3,
                            "placeName": {"default": "Team Charlie"},
                            "abbrev": "TC",
                            "commonName": {"default": "Charlie"},
                        },
                        "awayTeam": {
                            "id": 4,
                            "placeName": {"default": "Team Delta"},
                            "abbrev": "TD",
                            "commonName": {"default": "Delta"},
                        },
                    },
                ]
            }
        ]
    }
    mock_get_todays_schedule.return_value = mock_schedule_data

    expected_teams_data = [
        {
            "team_name": "Team Alpha",
            "team_abbr": "TA",
            "season": "20242025",
            "team_id": 1,
            "opponent_id": 2,
            "home": True,
            "tgpg": 0.0,
            "otga": 0.0,
            "otshga": 0.0,
        },
        {
            "team_name": "Team Bravo",
            "team_abbr": "TB",
            "season": "20242025",
            "team_id": 2,
            "opponent_id": 1,
            "home": False,
            "tgpg": 0.0,
            "otga": 0.0,
            "otshga": 0.0,
        },
        {
            "team_name": "Team Charlie",
            "team_abbr": "TC",
            "season": "20242025",
            "team_id": 3,
            "opponent_id": 4,
            "home": True,
            "tgpg": 0.0,
            "otga": 0.0,
            "otshga": 0.0,
        },
        {
            "team_name": "Team Delta",
            "team_abbr": "TD",
            "season": "20242025",
            "team_id": 4,
            "opponent_id": 3,
            "home": False,
            "tgpg": 0.0,
            "otga": 0.0,
            "otshga": 0.0,
        },
    ]
    expected_response = {"statusCode": 200, "teams": expected_teams_data}

    response = handle_get_teams({}, {})

    mock_get_todays_schedule.assert_called_once()
    mock_schedule_run.assert_called_once_with({"2025-06-11T01:00:00+00:00"})
    assert response == expected_response


@patch("service.schedule_run")
def test_handle_get_teams_no_teams(mock_schedule_run, mock_get_todays_schedule):
    mock_schedule_data = {"gameWeek": [{"games": []}]}  # No games scheduled
    mock_get_todays_schedule.return_value = mock_schedule_data

    expected_response = {"statusCode": 200, "teams": []}

    response = handle_get_teams({}, {})

    mock_get_todays_schedule.assert_called_once()
    assert response == expected_response


@patch("service.schedule_run")
def test_handle_get_teams_exception_in_get_teams(mock_schedule_run, mock_get_todays_schedule):
    mock_malformed_schedule_data = {"some_unexpected_key": []}
    mock_get_todays_schedule.return_value = mock_malformed_schedule_data

    with pytest.raises(KeyError):
        handle_get_teams({}, {})
