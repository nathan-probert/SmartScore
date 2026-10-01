"""HTTP client for the Cloudflare smartscore-api worker.

Replaces the legacy ``Api-{ENV}`` Lambda invocations (GET_DATES_NO_SCORED,
DELETE_GAME, POST_BACKFILL, GET_DATE, POST_BATCH, GET_ALL). Requests are
authenticated with a bearer token from the ``SMARTSCORE_API_TOKEN``
environment variable (see config.py). The worker side calls the same secret
``API_AUTH_TOKEN`` (smartscore-api wrangler secret).
"""

import base64
import json
import time

import requests
from aws_lambda_powertools import Logger

from config import SMARTSCORE_API_TOKEN
from constants import SMARTSCORE_API_BASE_URL

logger = Logger()

REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 4
BASE_RETRY_DELAY_SECONDS = 1
# 4xx responses are client errors and will not fix themselves, so only retry
# 5xx (and transport errors).
SERVER_ERROR_STATUS = 500


def _headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SMARTSCORE_API_TOKEN}",
    }


def _request(method, path, params=None, json_body=None):
    """Makes an authenticated request to the worker, retrying with exponential backoff.

    Mirrors the retry pattern used by ``utility.exponential_backoff_supabase_request``:
    a bounded number of attempts with ``base_delay * 2 ** attempt`` sleeps. Client
    errors (4xx) are not retried, since they will not fix themselves.
    """
    url = f"{SMARTSCORE_API_BASE_URL}{path}"

    for attempt in range(MAX_RETRIES):
        try:
            logger.info(f"Making {method} request to Cloudflare API: {url}")
            response = requests.request(
                method, url, headers=_headers(), params=params, json=json_body, timeout=REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code is not None and status_code < SERVER_ERROR_STATUS:
                logger.error(f"{method} {url} failed with status {status_code}, not retrying")
                raise
            if attempt == MAX_RETRIES - 1:
                logger.error(f"{method} {url} failed after {MAX_RETRIES} attempts: {e}")
                raise
            wait_time = BASE_RETRY_DELAY_SECONDS * (2**attempt)
            logger.warning(f"{method} {url} failed: {e}. Retrying in {wait_time} seconds...")
            time.sleep(wait_time)
        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                logger.error(f"{method} {url} failed after {MAX_RETRIES} attempts: {e}")
                raise
            wait_time = BASE_RETRY_DELAY_SECONDS * (2**attempt)
            logger.warning(f"{method} {url} failed: {e}. Retrying in {wait_time} seconds...")
            time.sleep(wait_time)

    raise Exception(f"Max retries reached for {method} {url}")


def get_unscored_dates():
    """GET /unscored-dates. Returns a list of YYYY-MM-DD strings."""
    return _request("GET", "/unscored-dates").get("dates", [])


def delete_game(date, home, away):
    """DELETE /game?date=&home=&away=. Returns the worker response dict."""
    return _request("DELETE", "/game", params={"date": date, "home": home, "away": away})


def backfill_scored(date, scored_player_ids):
    """POST /backfill-scored for a single date. Player IDs must be strings."""
    return _request("POST", "/backfill-scored", json_body={"date": date, "scoredPlayerIds": scored_player_ids})


def get_players_for_date(date):
    """GET /players?date=. Returns a list of player dicts."""
    return _request("GET", "/players", params={"date": date}).get("players", [])


def upload_players(players, date=None):
    """POST /players. Attaches ``date`` to each player when given.

    The worker expects the date inside each player object, while Step
    Functions passes it alongside the players list.
    """
    if date is not None:
        players = [{**player, "date": date} for player in players]
    return _request("POST", "/players", json_body={"players": players})


def get_all_players():
    """GET /all-players. Returns a list of player dicts.

    Unlike the legacy GET_ALL Lambda method (base64-encoded gzipped JSON
    array), the worker returns base64-encoded plain JSON of the form
    ``{"players": [...]}``.
    """
    data = _request("GET", "/all-players").get("data", "")
    decoded = base64.b64decode(data).decode("utf-8")
    return json.loads(decoded).get("players", [])
