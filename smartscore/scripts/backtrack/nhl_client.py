"""Throttled, cached HTTP client for the NHL public stats API.

Only the game-log endpoint is wrapped. It is what the whole backtrack depends on,
and it is the one that benefits most from a disk cache: a full run is on the order
of 10k requests, and re-running after a fix should not re-fetch what already
landed.

The cache is keyed on ``player-season-gametype`` and stores the parsed ``gameLog``
array. A cache hit means zero network calls, which is what makes a re-run cheap.

Nothing here ever expires. For a completed season that is safe - a finished game's
box score and a closed season's schedule are history. For a season still in
progress it is a trap: the schedule and game-log caches freeze mid-season, so a
stale entry misses games played after it was written. Delete the cache file (or
restrict to completed seasons) rather than trusting an in-progress one.

Rate limiting is a fixed sleep between requests rather than a token bucket. The
public endpoint has no documented quota, and a flat delay is predictable and easy
to reason about when a run takes hours. ``--delay`` exists so a run that gets
throttled can be slowed down without editing code.

Retries cover transport errors and 5xx. A 404 is not retried: it means the player
never appeared in that season, which is a fact, not a failure.
"""

import argparse
import json
import time
from pathlib import Path

import requests

BASE_URL = "https://api-web.nhle.com/v1"

# Regular season. Playoffs (gameType 3) are deliberately excluded: the stored
# Player-Snapshots values are regular-season season-to-date, and mixing in a
# playoff run inflates both numerator and denominator.
REGULAR_SEASON = 2

# schedule/{date} answers with the seven-day window containing that date, and
# reports regularSeasonStartDate / regularSeasonEndDate for the season the date
# falls in. So one call both locates the season bounds and starts the walk.
SCHEDULE_WINDOW_DAYS = 7

MAX_RETRIES = 4
BASE_RETRY_DELAY_SECONDS = 1

# The public endpoint has no published rate limit. This is deliberately gentle:
# a full four-season run is ~10k requests, so there is no reason to push it.
DEFAULT_DELAY_SECONDS = 0.34

CACHE_DIR = Path(__file__).parent / "cache"

# Last-request timestamp for the throttle, held in a one-element list rather than
# a module global so _throttle stays a plain function (ruff PLW0603).
_last_request_at = [0.0]

# Fields every cached box-score record must carry. A cached file missing any of
# these is treated as stale and re-fetched, so a cache written by an older version
# of this module is upgraded rather than trusted. Keeping this explicit means a new
# field added to fetch_boxscore cannot be silently omitted from a rebuild.
_REQUIRED_RECORD_FIELDS = ("game_id", "game_date", "player_id", "team_abbrev", "team_goals_for")


def _throttle(delay_seconds):
    """Sleep so consecutive requests are at least ``delay_seconds`` apart."""
    elapsed = time.monotonic() - _last_request_at[0]
    if elapsed < delay_seconds:
        time.sleep(delay_seconds - elapsed)

    _last_request_at[0] = time.monotonic()


def _cache_path(cache_dir, player_id, season, game_type):
    return Path(cache_dir) / f"{player_id}-{season}-{game_type}.json"


def _cache_load(path):
    if not path.exists():
        return None

    try:
        with path.open(encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        # A truncated cache file from an interrupted run should re-fetch, not
        # abort the run.
        return None

    return payload.get("gameLog", [])


def _cache_store(path, game_log):
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file then rename, so an interrupt mid-write cannot leave a
    # half-written file that reads as a valid empty cache entry.
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({"gameLog": game_log}, f)
    tmp.replace(path)


def fetch_game_log(
    player_id,
    season,
    game_type=REGULAR_SEASON,
    delay_seconds=DEFAULT_DELAY_SECONDS,
    cache_dir=CACHE_DIR,
    use_cache=True,
):
    """Return one player's game log as a list of game dicts.

    ``season`` is the NHL season id ("20232024"), not a calendar year.

    Returns an empty list when the player has no games in that season - a 404 is
    the normal answer for a player who did not play, and is treated as data rather
    than an error.
    """
    path = _cache_path(cache_dir, player_id, season, game_type)

    if use_cache:
        cached = _cache_load(path)
        if cached is not None:
            return cached

    url = f"{BASE_URL}/player/{player_id}/game-log/{season}/{game_type}"

    for attempt in range(MAX_RETRIES):
        _throttle(delay_seconds)

        try:
            response = requests.get(url, timeout=30)
        except requests.RequestException:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(BASE_RETRY_DELAY_SECONDS * (2**attempt))
            continue

        if response.status_code == 404:
            # No games in this season. Not an error; cache it so a re-run stops
            # asking about a player who did not play.
            _cache_store(path, [])
            return []

        if response.status_code >= 500:
            if attempt == MAX_RETRIES - 1:
                response.raise_for_status()
            time.sleep(BASE_RETRY_DELAY_SECONDS * (2**attempt))
            continue

        response.raise_for_status()
        game_log = response.json().get("gameLog", [])
        _cache_store(path, game_log)
        return game_log

    return []


def fetch_schedule(date, delay_seconds=DEFAULT_DELAY_SECONDS):
    """The seven-day schedule window containing ``date``.

    Returns the raw payload. Two fields matter to callers:
    ``regularSeasonStartDate`` / ``regularSeasonEndDate`` (the season bounds, for
    walking the whole season) and ``gameWeek[].games[]`` (the games themselves).
    """
    _throttle(delay_seconds)
    response = requests.get(f"{BASE_URL}/schedule/{date}", timeout=30)
    response.raise_for_status()

    return response.json()


def season_games(season, delay_seconds=DEFAULT_DELAY_SECONDS, cache_dir=CACHE_DIR):
    """Every regular-season game id in ``season``, walking the schedule in windows.

    The schedule endpoint only serves seven days at a time, so a season is
    assembled by repeatedly asking for the window after the last one seen. The
    walk advances by ``nextStartDate`` rather than by adding seven days, because a
    season does not start on a fixed day and a fixed stride would eventually skip
    or repeat games.

    Returns a list of dicts with ``id``, ``date``, ``away``, ``home``.
    """
    path = Path(cache_dir) / f"schedule-{season}.json"

    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # A schedule lists every fixture for the season, including ones that have not been
    # played. For an in-progress season that is the large majority - 2026-07 on
    # 2026-10-08 returns 46 games of which 34 are FUT. Fetching box scores for
    # those is wasted requests against a payload that has no roster yet, so the
    # walk keeps only games the schedule reports as having been played.
    #
    # OFF and FINAL both mean played (the API uses both); anything else, including
    # FUT and an in-flight LIVE, is skipped.
    _UNPLAYED_GAME_STATES = {"FUT", "TBD", "PPD"}

    # The seed must fall INSIDE the requested season. schedule/ resolves its
    # bounds from whichever season contains the date it is given, so seeding on
    # calendar year "2023" for season 20232024 lands in 2022-23 and silently
    # returns the wrong 600+ games. A mid-season date is unambiguous.
    seed = f"{int(season[:4]) + 1}-01-15"
    first = fetch_schedule(seed, delay_seconds=delay_seconds)

    start = first.get("regularSeasonStartDate")
    end = first.get("regularSeasonEndDate")

    if not start or not end:
        raise ValueError(f"schedule did not report season bounds for {season}")

    games = {}
    cursor = start

    while cursor <= end:
        # Always fetch the window for the cursor. `first` was fetched for the SEED
        # date, not for `start`, so reusing it here would only ever collect the
        # seed's week and skip everything before it.
        payload = fetch_schedule(cursor, delay_seconds=delay_seconds)

        for week in payload.get("gameWeek") or []:
            week_date = week.get("date")

            for game in week.get("games") or []:
                # gameType 2 is regular season; 3 is playoffs, 1 is preseason.
                if game.get("gameType") != REGULAR_SEASON:
                    continue

                # The 7-day windows either side of the season boundary carry games
                # from the neighbouring season. Filtering on the explicit `season`
                # field is what keeps a single season's walk from absorbing them;
                # the date bounds alone do not, because October and April windows
                # straddle two seasons.
                if str(game.get("season")) != str(season):
                    continue

                game_id = game.get("id")
                if not game_id or game_id in games:
                    continue

                if str(game.get("gameState", "")).upper() in _UNPLAYED_GAME_STATES:
                    continue

                games[game_id] = {
                    "id": game_id,
                    "date": week_date,
                    "away": (game.get("awayTeam") or {}).get("abbrev"),
                    "home": (game.get("homeTeam") or {}).get("abbrev"),
                }

        next_start = payload.get("nextStartDate")

        # Guard against a payload that does not advance, which would loop forever.
        if not next_start or next_start <= cursor:
            break

        cursor = next_start

    result = sorted(games.values(), key=lambda g: (g["date"] or "", g["id"]))

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(result, f)

    return result


def fetch_boxscore(game_id, delay_seconds=DEFAULT_DELAY_SECONDS, cache_dir=CACHE_DIR):
    """One game's box score: both teams' full player roster.

    This is the roster discovery source. Every skater who appeared is present in
    the payload, so the player list comes from the games themselves rather than
    from a table of who we happened to pick before - which is the whole point, since
    a new player picked tomorrow has to appear.

    Normalised to a list of records, one per player appearance::

        {player_id, name, team_abbrev, position, sweater_number, home,
         goals, assists, points, shots, pim, toi, plus_minus, shifts,
         power_play_goals}

    Note what is NOT here: shorthanded goals, OT goals, game-winning goals and
    power-play points are absent from this payload, though the per-player game log
    has all four. So the boxscore establishes *who played* and *for whom*, while
    the game log supplies the full stat set. See reconstruct.py.
    """
    path = Path(cache_dir) / f"boxscore-{game_id}.json"

    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                cached = json.load(f)

            # Older cache files predate fields added later. They are still valid
            # data for the fields they do carry, so treat a record missing a newly
            # added one as stale and re-fetch rather than returning a half-upgraded
            # payload - a cache that silently lacks team_goals_for would make
            # team goal totals fall back to summing players, which is the bug
            # team_goals_for exists to prevent.
            if cached and not cached[0].get("game_id"):
                cached = None
            elif cached and _REQUIRED_RECORD_FIELDS and not all(f in cached[0] for f in _REQUIRED_RECORD_FIELDS):
                cached = None

            if cached is not None:
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    _throttle(delay_seconds)
    response = requests.get(f"{BASE_URL}/gamecenter/{game_id}/boxscore", timeout=30)
    response.raise_for_status()
    payload = response.json()

    # The team objects carry the official score for each side. This is the
    # authoritative team goal total and it does NOT always equal the sum of the
    # skaters' `goals` in this same payload - two goal types are attributed to a
    # team without appearing in any skater's boxscore row:
    #
    #   * shootout game-winning goals - counted on the scoreboard and in the team
    #     total, but the play-by-play carries only a shootout-complete marker
    #   * goalie empty-net goals - officially credited to the goalie, omitted from
    #     his `goals` field
    #
    # Verified on 2023020442 (PIT at MTL, shootout): official 4, skater sum 3.
    # So team goals must come from here, never from summing players.
    team_scores = {
        "away": ((payload.get("awayTeam") or {}).get("abbrev"), (payload.get("awayTeam") or {}).get("score")),
        "home": ((payload.get("homeTeam") or {}).get("abbrev"), (payload.get("homeTeam") or {}).get("score")),
    }

    records = []

    # The payload keys the stat blocks by side, and the side tells us home/road,
    # so the two are read together rather than independently.
    for side, team in (("homeTeam", payload.get("homeTeam")), ("awayTeam", payload.get("awayTeam"))):
        abbrev = (team or {}).get("abbrev")
        stats = (payload.get("playerByGameStats") or {}).get(side) or {}
        goals_for = team_scores["home" if side == "homeTeam" else "away"][1]

        for group in ("forwards", "defense", "goalies"):
            for player in stats.get(group) or []:
                player_id = player.get("playerId")

                if not player_id:
                    continue

                records.append(
                    {
                        # Carried on the record so a cached box score is
                        # self-describing: the raw store keys on (player_id, game_id)
                        # and does not have to join back to the schedule to get it.
                        "game_id": game_id,
                        "game_date": payload.get("gameDate"),
                        "player_id": player_id,
                        "name": (player.get("name") or {}).get("default"),
                        "team_abbrev": abbrev,
                        # The official team goal total for this game, repeated on
                        # each player row. Duplicated deliberately: it is a
                        # property of the team-game, and compute_derived_team reads
                        # it once per (team, game) rather than summing rows.
                        "team_goals_for": goals_for,
                        "position": player.get("position"),
                        "sweater_number": player.get("sweaterNumber"),
                        "home": side == "homeTeam",
                        "goals": player.get("goals") or 0,
                        "assists": player.get("assists") or 0,
                        "points": player.get("points") or 0,
                        "shots": player.get("sog") or 0,
                        "pim": player.get("pim") or 0,
                        "toi": player.get("toi"),
                        "plus_minus": player.get("plusMinus"),
                        "shifts": player.get("shifts"),
                        "power_play_goals": player.get("powerPlayGoals") or 0,
                    }
                )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(records, f)

    return records


def fetch_player_name(player_id, delay_seconds=DEFAULT_DELAY_SECONDS):
    """Fetch a player's display name from the landing endpoint.

    Not cached: it is one extra request per player against thousands of game-log
    requests, and the name rarely changes.
    """
    _throttle(delay_seconds)

    try:
        response = requests.get(f"{BASE_URL}/player/{player_id}/landing", timeout=30)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        return None

    first = (payload.get("firstName") or {}).get("default")
    last = (payload.get("lastName") or {}).get("default")

    if not first and not last:
        return None

    return f"{first or ''} {last or ''}".strip()


def main():
    parser = argparse.ArgumentParser(description="Smoke-test the NHL game-log client.")
    parser.add_argument("player_id", type=int)
    parser.add_argument("--season", default="20232024")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    args = parser.parse_args()

    game_log = fetch_game_log(args.player_id, args.season, delay_seconds=args.delay)
    print(f"player {args.player_id} {args.season}: {len(game_log)} game(s)")
    print(f"name: {fetch_player_name(args.player_id, delay_seconds=args.delay)}")

    for game in game_log[:5]:
        # `or ""` before the width spec: some rows carry no teamAbbrev (ARI was
        # missing this way), and formatting None with :>4 raises TypeError.
        print(
            f"  {game['gameDate']} {(game.get('teamAbbrev') or ''):>4} vs "
            f"{(game.get('opponentAbbrev') or ''):<4} {game.get('homeRoadFlag')} "
            f"G{game.get('goals')} A{game.get('assists')} "
            f"TOI {game.get('toi')} shots {game.get('shots')}"
        )


if __name__ == "__main__":
    main()
