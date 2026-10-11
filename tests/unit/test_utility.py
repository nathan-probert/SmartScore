from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

from config import ENV
from utility import (
    count_picks_for_date,
    create_cron_schedule,
    exponential_backoff_request,
    exponential_backoff_supabase_request,
    get_picks_for_date,
)


@patch("utility.SUPABASE_CLIENT")
def test_supabase_count_only_returns_row_count(mock_client):
    """count_only issues a HEAD request and returns the count, not the rows."""
    execute = mock_client.table.return_value.select.return_value.eq.return_value.execute
    execute.return_value.count = 7

    result = exponential_backoff_supabase_request(
        f"Picks-{ENV}", select="id", eq=("date", "2026-10-11"), count_only=True
    )

    assert result == 7
    mock_client.table.return_value.select.assert_called_once_with("id", count="exact", head=True)
    mock_client.table.return_value.select.return_value.eq.assert_called_once_with("date", "2026-10-11")


@patch("utility.exponential_backoff_supabase_request")
def test_count_picks_for_date_uses_count_only(mock_request):
    """The run-status check must not download the roster."""
    mock_request.return_value = 3

    assert count_picks_for_date("2026-10-11") == 3
    mock_request.assert_called_once_with(f"Picks-{ENV}", select="id", eq=("date", "2026-10-11"), count_only=True)


@patch("utility.exponential_backoff_supabase_request")
def test_get_picks_for_date_filters_on_date(mock_request):
    """Emails read today's published rows, not the whole table."""
    mock_request.return_value = [{"name": "Player 1"}]

    result = get_picks_for_date("2026-10-11")

    assert result == [{"name": "Player 1"}]
    mock_request.assert_called_once_with(f"Picks-{ENV}", eq=("date", "2026-10-11"))


@patch("smartscore_info_client.utility.requests.get")
def test_exponential_backoff_request_success(mock_get):
    """Test successful GET request."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": "success"}
    mock_get.return_value = mock_response

    result = exponential_backoff_request("http://test.com")

    assert result == {"data": "success"}
    mock_get.assert_called_once()


@patch("smartscore_info_client.utility.requests.post")
def test_exponential_backoff_request_post_success(mock_post):
    """Test successful POST request."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"status": "created"}
    mock_post.return_value = mock_response

    result = exponential_backoff_request("http://test.com", method="post", json_data={"key": "value"})

    assert result == {"status": "created"}
    mock_post.assert_called_once()


@patch("smartscore_info_client.utility.requests.post")
def test_exponential_backoff_request_with_form_data(mock_post):
    """Test POST request with form data."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"status": "created"}
    mock_post.return_value = mock_response

    result = exponential_backoff_request("http://test.com", method="post", data={"form": "data"})

    assert result == {"status": "created"}
    assert mock_post.call_args[1]["data"] == {"form": "data"}


@patch("smartscore_info_client.utility.time.sleep")
@patch("smartscore_info_client.utility.requests.get")
def test_exponential_backoff_request_retry(mock_get, mock_sleep):
    """Test retry logic on failure."""
    mock_get.side_effect = [
        requests.exceptions.Timeout("Timeout"),
        requests.exceptions.Timeout("Timeout"),
        MagicMock(json=lambda: {"data": "success"}),
    ]

    result = exponential_backoff_request("http://test.com", max_retries=5)

    assert result == {"data": "success"}
    assert mock_get.call_count == 3
    assert mock_sleep.call_count == 2
    # Check exponential backoff delays
    assert mock_sleep.call_args_list[0][0][0] == 1  # 1 * 2^0
    assert mock_sleep.call_args_list[1][0][0] == 2  # 1 * 2^1


@patch("smartscore_info_client.utility.time.sleep")
@patch("smartscore_info_client.utility.requests.get")
def test_exponential_backoff_request_max_retries_exceeded(mock_get, mock_sleep):
    """Test when max retries are exceeded."""
    mock_get.side_effect = requests.exceptions.Timeout("Timeout")

    with pytest.raises(Exception, match="Max retries reached"):
        exponential_backoff_request("http://test.com", max_retries=3)

    assert mock_get.call_count == 3
    assert mock_sleep.call_count == 3


@patch("smartscore_info_client.utility.requests.get")
def test_exponential_backoff_request_with_headers(mock_get):
    """Test request with custom headers."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": "success"}
    mock_get.return_value = mock_response

    headers = {"Authorization": "Bearer token"}
    result = exponential_backoff_request("http://test.com", headers=headers)

    assert result == {"data": "success"}
    assert mock_get.call_args[1]["headers"] == headers


def test_exponential_backoff_request_invalid_method():
    """Test with invalid HTTP method."""
    with pytest.raises(ValueError, match="Unsupported HTTP method"):
        exponential_backoff_request("http://test.com", method="delete")


@patch("smartscore_info_client.utility.time.sleep")
@patch("smartscore_info_client.utility.requests.get")
def test_exponential_backoff_request_connection_error(mock_get, mock_sleep):
    """Test retry on connection error."""
    mock_get.side_effect = [
        requests.exceptions.ConnectionError("Connection failed"),
        MagicMock(json=lambda: {"data": "success"}),
    ]

    result = exponential_backoff_request("http://test.com", max_retries=3)

    assert result == {"data": "success"}
    assert mock_get.call_count == 2


@patch("smartscore_info_client.utility.time.sleep")
@patch("smartscore_info_client.utility.requests.get")
def test_exponential_backoff_request_http_error(mock_get, mock_sleep):
    """Test retry on HTTP error status."""
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("500 Server Error")

    mock_get.side_effect = [
        mock_response,
        MagicMock(json=lambda: {"data": "success"}),
    ]

    result = exponential_backoff_request("http://test.com", max_retries=3)

    assert result == {"data": "success"}


@patch("smartscore_info_client.utility.time.sleep")
@patch("smartscore_info_client.utility.requests.get")
def test_exponential_backoff_request_custom_base_delay(mock_get, mock_sleep):
    """Test custom base delay."""
    mock_get.side_effect = [
        requests.exceptions.Timeout("Timeout"),
        MagicMock(json=lambda: {"data": "success"}),
    ]

    result = exponential_backoff_request("http://test.com", base_delay=2, max_retries=3)

    assert result == {"data": "success"}
    # Check that custom base delay is used: 2 * 2^0 = 2
    assert mock_sleep.call_args_list[0][0][0] == 2


def test_create_cron_schedule_basic():
    """Test basic cron schedule creation."""
    dt = datetime(2024, 1, 15, 14, 30, 0)

    result = create_cron_schedule(dt)

    assert result == "cron(30 14 15 1 ? 2024)"


def test_create_cron_schedule_midnight():
    """Test cron schedule at midnight."""
    dt = datetime(2024, 12, 31, 0, 0, 0)

    result = create_cron_schedule(dt)

    assert result == "cron(0 0 31 12 ? 2024)"


def test_create_cron_schedule_single_digits():
    """Test cron schedule with single digit values."""
    dt = datetime(2024, 3, 5, 8, 5, 0)

    result = create_cron_schedule(dt)

    assert result == "cron(5 8 5 3 ? 2024)"


def test_create_cron_schedule_end_of_month():
    """Test cron schedule on last day of month."""
    dt = datetime(2024, 2, 29, 23, 59, 0)  # Leap year

    result = create_cron_schedule(dt)

    assert result == "cron(59 23 29 2 ? 2024)"


def test_create_cron_schedule_new_year():
    """Test cron schedule on New Year's Day."""
    dt = datetime(2025, 1, 1, 0, 0, 0)

    result = create_cron_schedule(dt)

    assert result == "cron(0 0 1 1 ? 2025)"
