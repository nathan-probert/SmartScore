import base64
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

import cloudflare_client


def _response(payload):
    response = MagicMock()
    response.json.return_value = payload
    return response


def test_request_sends_bearer_token():
    """Every worker call carries the SMARTSCORE_API_TOKEN bearer header."""
    with (
        patch("cloudflare_client.SMARTSCORE_API_TOKEN", "test-token"),
        patch("cloudflare_client.requests.request", return_value=_response({"ok": True})) as mock_request,
    ):
        assert cloudflare_client._request("GET", "/health") == {"ok": True}

    args, kwargs = mock_request.call_args
    assert args[0] == "GET"
    assert args[1] == f"{cloudflare_client.SMARTSCORE_API_BASE_URL}/health"
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert kwargs["timeout"] == cloudflare_client.REQUEST_TIMEOUT_SECONDS


def test_get_unscored_dates():
    with patch("cloudflare_client._request", return_value={"dates": ["2026-04-15"]}) as mock_request:
        assert cloudflare_client.get_unscored_dates() == ["2026-04-15"]

    mock_request.assert_called_once_with("GET", "/unscored-dates")


def test_get_unscored_dates_missing_key():
    with patch("cloudflare_client._request", return_value={}):
        assert cloudflare_client.get_unscored_dates() == []


def test_delete_game_passes_query_params():
    with patch("cloudflare_client._request", return_value={"deleted": True}) as mock_request:
        assert cloudflare_client.delete_game("2026-04-15", "TOR", "MTL") == {"deleted": True}

    mock_request.assert_called_once_with("DELETE", "/game", params={"date": "2026-04-15", "home": "TOR", "away": "MTL"})


def test_backfill_scored_body():
    with patch("cloudflare_client._request", return_value={"scoredCount": 2}) as mock_request:
        assert cloudflare_client.backfill_scored("2026-04-15", ["1", "2"]) == {"scoredCount": 2}

    mock_request.assert_called_once_with(
        "POST", "/backfill-scored", json_body={"date": "2026-04-15", "scoredPlayerIds": ["1", "2"]}
    )


def test_get_players_for_date():
    players = [{"id": 1, "scored": True}]
    with patch("cloudflare_client._request", return_value={"date": "2026-04-15", "players": players}) as mock_request:
        assert cloudflare_client.get_players_for_date("2026-04-15") == players

    mock_request.assert_called_once_with("GET", "/players", params={"date": "2026-04-15"})


def test_get_players_for_date_missing_key():
    with patch("cloudflare_client._request", return_value={}):
        assert cloudflare_client.get_players_for_date("2026-04-15") == []


def test_upload_players_attaches_date():
    players = [{"id": 1, "name": "Player One"}]
    with patch("cloudflare_client._request", return_value={"insertedCount": 1}) as mock_request:
        cloudflare_client.upload_players(players, date="2026-04-15")

    mock_request.assert_called_once_with(
        "POST", "/players", json_body={"players": [{"id": 1, "name": "Player One", "date": "2026-04-15"}]}
    )
    # The caller's list is left untouched.
    assert players == [{"id": 1, "name": "Player One"}]


def test_upload_players_without_date_passes_through():
    players = [{"id": 1, "date": "2026-04-15"}]
    with patch("cloudflare_client._request", return_value={"insertedCount": 1}) as mock_request:
        cloudflare_client.upload_players(players)

    mock_request.assert_called_once_with("POST", "/players", json_body={"players": players})


def test_get_all_players_decodes_base64():
    players = [{"name": "Player One", "scored": 0}]
    encoded = base64.b64encode(json.dumps({"players": players}).encode("utf-8")).decode("utf-8")
    with patch("cloudflare_client._request", return_value={"data": encoded}):
        assert cloudflare_client.get_all_players() == players


def test_request_retries_then_succeeds():
    """A transient 500 is retried with backoff before giving up."""
    responses = [
        requests.exceptions.HTTPError(response=MagicMock(status_code=500)),
        _response({"dates": []}),
    ]

    def fake_request(*args, **kwargs):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with (
        patch("cloudflare_client.requests.request", side_effect=fake_request) as mock_request,
        patch("cloudflare_client.time.sleep") as mock_sleep,
    ):
        assert cloudflare_client._request("GET", "/unscored-dates") == {"dates": []}

    assert mock_request.call_count == 2
    mock_sleep.assert_called_once_with(cloudflare_client.BASE_RETRY_DELAY_SECONDS)


def test_request_does_not_retry_client_errors():
    """A 400 will not fix itself, so it raises immediately."""
    with (
        patch(
            "cloudflare_client.requests.request",
            side_effect=requests.exceptions.HTTPError(response=MagicMock(status_code=400)),
        ) as mock_request,
        patch("cloudflare_client.time.sleep") as mock_sleep,
        pytest.raises(requests.exceptions.HTTPError),
    ):
        cloudflare_client._request("POST", "/players", json_body={})

    assert mock_request.call_count == 1
    mock_sleep.assert_not_called()


def test_request_raises_after_max_retries():
    with (
        patch("cloudflare_client.requests.request", side_effect=requests.exceptions.ConnectionError("boom")),
        patch("cloudflare_client.time.sleep"),
    ):
        with pytest.raises(requests.exceptions.ConnectionError):
            cloudflare_client._request("GET", "/unscored-dates")
