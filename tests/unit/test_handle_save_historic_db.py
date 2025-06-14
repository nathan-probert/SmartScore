from unittest.mock import patch

from event_handler import handle_save_historic_db
from service import choose_picks, get_date


@patch("service.invoke_lambda")
@patch("service.update_historical_data")
@patch("service.get_historical_data")
def test_handle_save_historic_db_with_players(
    mock_get_historical_data, mock_update_historical_data, mock_invoke_lambda, players_input, old_entries
):
    today = get_date()
    picks = choose_picks(players_input)
    mock_get_historical_data.return_value = old_entries
    mock_invoke_lambda.return_value = {"body": "[]"}

    event = {"players": players_input}
    context = {}
    response = handle_save_historic_db(event, context)

    mock_update_historical_data.assert_called_once()
    called_arg = mock_update_historical_data.call_args[0][0]

    todays_entries = [entry for entry in called_arg if entry.get("date") == today]
    assert len(todays_entries) == len(picks)  # 3, one for each tims group

    assert len(called_arg) == 21  # 3 * 7 days
    assert response == {"statusCode": 200, "players": players_input}


@patch("service.invoke_lambda")
@patch("service.update_historical_data")
@patch("service.get_historical_data")
def test_handle_save_historic_db_with__no_players(
    mock_get_historical_data, mock_update_historical_data, mock_invoke_lambda, old_entries
):
    today = get_date()
    mock_get_historical_data.return_value = old_entries
    mock_invoke_lambda.return_value = {"body": "[]"}

    event = {"players": []}
    context = {}
    response = handle_save_historic_db(event, context)

    mock_update_historical_data.assert_called_once()
    called_arg = mock_update_historical_data.call_args[0][0]

    todays_entries = [entry for entry in called_arg if entry.get("date") == today]
    assert len(todays_entries) == 0

    assert len(called_arg) == 21  # 3 * 7 days
    assert response == {"statusCode": 200, "players": []}
