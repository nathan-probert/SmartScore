"""Historic tims (challenge pick groups) from api.hockeychallengehelper.com.

``tims`` on Player-Snapshots is which of the challenge's three daily pick
groups a player belonged to that day (1-3, 0 when unlisted) - the same groups
``smartscore.utility.get_tims_players`` reads live from ``/api/picks``. The
live endpoint only answers for today, but ``/api/history?datetime=YYYY-MM-DD``
serves the same ``playerLists`` shape for past dates, verified back to
2023-10-10 and into the 2024 playoffs.

The host 403s header-less requests, so the fetch carries the same
Origin/Referer the web app sends. Offseason dates answer 404, which is cached
as an empty group list rather than raised - a season's game dates always have
data, but a caller walking a wide date range should not die on July.
"""

import json
from pathlib import Path

import requests
from nhl_client import CACHE_DIR, DEFAULT_DELAY_SECONDS, _get_json, season_games

HISTORY_URL = "https://api.hockeychallengehelper.com/api/history"

HEADERS = {
    "Origin": "https://hockeychallengehelper.com",
    "Referer": "https://hockeychallengehelper.com/",
    "User-Agent": "Mozilla/5.0",
}


def fetch_history(date_str, delay_seconds=DEFAULT_DELAY_SECONDS, cache_dir=CACHE_DIR):
    """One date's pick groups; cached as ``tims-{date}.json``.

    Returns the raw payload: ``{"date", "availableTimes", "playerLists":
    [{"id": 1|2|3, "players": [{"nhlPlayerId", "fullName", ...}]}]}``.
    """
    path = Path(cache_dir) / f"tims-{date_str}.json"

    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    try:
        payload = _get_json(f"{HISTORY_URL}?datetime={date_str}", delay_seconds, headers=HEADERS)
    except requests.HTTPError as error:
        if error.response is None or error.response.status_code != 404:
            raise
        payload = {"date": date_str, "playerLists": []}

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f)
    tmp.replace(path)

    return payload


def parse_history(payload):
    """One history payload -> ``{player_id: {"group", "scored"}}``.

    ``group`` is the challenge's pick group (1-3) and ``scored`` the helper's
    own grading of whether that player scored that night - kept alongside the
    group so the local store can persist both from one walk of the payloads.

    Shared by tims_map (the fetch path) and local_store's re-parse of the
    committed tims_raw payloads, so a parse fix needs exactly one edit.
    """
    groups = {}

    for group in payload.get("playerLists") or []:
        group_id = group.get("id")
        for player in group.get("players") or []:
            player_id = player.get("nhlPlayerId")
            if player_id and group_id:
                groups[player_id] = {
                    "group": group_id,
                    "scored": player.get("scored"),
                }

    return groups


def tims_map(season, delay_seconds=DEFAULT_DELAY_SECONDS, cache_dir=CACHE_DIR):
    """``{game_date: {player_id: {"group", "scored"}}}`` for a season's games."""
    games = season_games(season, delay_seconds=delay_seconds, cache_dir=cache_dir)
    dates = sorted({game["date"] for game in games if game.get("date")})

    result = {}
    for date_str in dates:
        payload = fetch_history(date_str, delay_seconds=delay_seconds, cache_dir=cache_dir)
        result[date_str] = parse_history(payload)

    return result
