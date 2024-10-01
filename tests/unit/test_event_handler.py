from unittest.mock import patch


@patch("service.get_players_from_team")
@patch("service.get_teams")
@patch("service.get_todays_schedule")
def test_handle_get_players_for_date(mock_get_todays_schedule, mock_get_teams, mock_get_players_from_team):
    from smartscore.event_handler import handle_get_players_for_date

    mock_get_todays_schedule.return_value = {}
    mock_get_teams.return_value = []
    mock_get_players_from_team.return_value = []

    response = handle_get_players_for_date(None, None)
    assert response["statusCode"] == 200
    assert response["body"] is not None
