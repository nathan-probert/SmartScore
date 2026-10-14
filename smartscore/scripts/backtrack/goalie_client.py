"""Historic projected starting goalies from Rotowire's tables API.

The live pipeline scrapes the same table for today (``service.
get_starting_goalies``) and denormalizes the opponent's starter onto every
skater row as ``opp_goalie_name/team/status/confirmed``. The ``date``
parameter serves history - verified back to 2023-10-10 and into the 2024
playoffs - and offseason dates answer 200 with an empty list rather than a
404, so nothing needs special-casing for July.

This source provides identity and status only. The ``opp_goalie_gaa/save_pct/
record/shutouts/games_played`` stat columns are reconstructed separately in
``local_store.compute_goalie_features`` from the NHL game logs already cached
by --build - strictly-before each date, the same as-of-morning view a live
``club-stats`` scrape captured, and team-scoped like club-stats itself.
``opp_goalie_nhl_id`` is the matched goalie's player_id from those rows.
"""

import json
from pathlib import Path

from nhl_client import CACHE_DIR, DEFAULT_DELAY_SECONDS, _get_json, season_games

ROTOWIRE_GOALIE_URL = "https://www.rotowire.com/hockey/tables/projected-goalies.php"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}

# RotoWire abbreviations that differ from the NHL API's, mirroring
# service.ROTOWIRE_TEAM_ABBR_MAP - the store keys everything by NHL
# abbreviations, so the goalie map must match them.
TEAM_ABBR_MAP = {
    "MON": "MTL",
    "LAS": "VGK",
}


def normalize_team_abbr(abbr):
    """Uppercase and translate a RotoWire abbreviation to the NHL API's."""
    if not abbr:
        return ""
    return TEAM_ABBR_MAP.get(abbr.upper(), abbr.upper())


def fetch_goalies(date_str, delay_seconds=DEFAULT_DELAY_SECONDS, cache_dir=CACHE_DIR):
    """One date's projected goalies; cached as ``goalies-{date}.json``.

    Returns the raw payload: a list of game dicts, each with
    ``{home,visit}{team,Player,Status}`` (RotoWire's casing). Offseason
    dates are an empty list.
    """
    path = Path(cache_dir) / f"goalies-{date_str}.json"

    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    payload = _get_json(f"{ROTOWIRE_GOALIE_URL}?date={date_str}", delay_seconds, headers=HEADERS)

    if not isinstance(payload, list):
        # A table endpoint answering with an object means the shape changed;
        # caching it would poison every downstream publish.
        raise ValueError(f"unexpected goalies payload type for {date_str}: {type(payload).__name__}")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f)
    tmp.replace(path)

    return payload


def goalies_map(season, delay_seconds=DEFAULT_DELAY_SECONDS, cache_dir=CACHE_DIR):
    """``{game_date: {team_abbr: {"name", "status"}}}`` for a season's games.

    Teams without a listed starter that date are simply absent, mirroring the
    live product, which writes status UNKNOWN when the table has no row.
    """
    games = season_games(season, delay_seconds=delay_seconds, cache_dir=cache_dir)
    dates = sorted({game["date"] for game in games if game.get("date")})

    result = {}
    for date_str in dates:
        payload = fetch_goalies(date_str, delay_seconds=delay_seconds, cache_dir=cache_dir)
        by_team = {}

        for game in payload:
            if not isinstance(game, dict):
                continue
            for side in ("home", "visit"):
                team = normalize_team_abbr(game.get(f"{side}team"))
                name = (game.get(f"{side}Player") or "").strip()
                if not team or not name:
                    continue
                by_team[team] = {
                    "name": name,
                    "status": (game.get(f"{side}Status") or "").strip(),
                }

        result[date_str] = by_team

    return result
