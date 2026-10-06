import datetime
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

import make_predictions_rust
import pytz
import requests
from aws_lambda_powertools import Logger
from smartscore_info_client.api.nhle import NHLClient
from smartscore_info_client.models.player import Player, PlayerInfo
from smartscore_info_client.models.team import GameTeam, TeamInfo
from smartscore_info_client.utility import exponential_backoff_request

from constants import DAYS_TO_KEEP_HISTORIC_DATA, NUM_EXPECTED_PLAYERS, WEIGHTS
from email_utility import send_email
from feature_flags import NHL_MOCK_FLAG, is_feature_enabled
from mock_nhl_client import MockNHLClient
from nhl_lineups import normalize_player_name
from player_archive import backfill_scored, delete_game_snapshots, get_players_for_date, get_unscored_dates
from utility import (
    get_cur_pick_pct,
    get_emails,
    get_historical_data,
    get_season_id,
    get_season_pick_pct,
    get_tims_players,
    get_today_db,
    save_to_db,
    schedule_run,
    update_historical_data,
    upload_metrics,
    upload_season_metrics,
)

logger = Logger()


def get_nhl_client():
    """Return the NHL client appropriate for the current feature flag state.

    When the ``mock-nhl-api`` flag is enabled (e.g. for off-season dev work
    or integration tests), a :class:`MockNHLClient` serving frozen fixtures is
    returned instead of the live ``NHLClient``.
    """
    if is_feature_enabled(NHL_MOCK_FLAG):
        return MockNHLClient()
    return NHLClient()


def get_date(hour=False, add_days=0, subtract_days=0):
    toronto_tz = pytz.timezone("America/Toronto")
    date = datetime.datetime.now(toronto_tz)
    if add_days:
        date += datetime.timedelta(days=add_days)
    if subtract_days:
        date -= datetime.timedelta(days=subtract_days)

    if hour:
        return date.strftime("%Y-%m-%dT%H:%M:%S")
    return date.strftime("%Y-%m-%d")


def get_todays_schedule():
    date = get_date()
    logger.info(f"Getting players for date: {date}")

    return get_nhl_client().get_schedule(date)


def get_teams(data):
    games = data["gameWeek"][0]["games"]

    teams = []
    start_times = set()
    for game in games:
        start_times.add(game["startTimeUTC"])

        home_name = game["homeTeam"]["placeName"]["default"]
        if home_name == " ":
            home_name = game["homeTeam"]["commonName"]["default"]

        away_name = game["awayTeam"]["placeName"]["default"]
        if away_name == " ":
            away_name = game["awayTeam"]["commonName"]["default"]

        home_team = GameTeam(
            team_name=home_name,
            team_abbr=game["homeTeam"]["abbrev"],
            season=game["season"],
            team_id=game["homeTeam"]["id"],
            opponent_id=game["awayTeam"]["id"],
            home=True,
        )
        away_team = GameTeam(
            team_name=away_name,
            team_abbr=game["awayTeam"]["abbrev"],
            season=game["season"],
            team_id=game["awayTeam"]["id"],
            opponent_id=game["homeTeam"]["id"],
            home=False,
        )

        teams.append(home_team)
        teams.append(away_team)

    if not start_times:
        logger.info("No start times found")
    else:
        schedule_run(start_times)

    return teams


def enrich_teams(teams):
    """Attach team stats to each game team, fetched once per season."""
    nhl_client = get_nhl_client()
    return [
        TeamInfo(
            team=team,
            stats=nhl_client.get_team_stats(team.season, team.team_id, team.opponent_id),
        )
        for team in teams
    ]


def get_players_from_team(team):
    players = []
    nhl_client = get_nhl_client()

    roster = nhl_client.get_roster(team.team_abbr)

    player_types = ["forwards", "defensemen"]
    for player_type in player_types:
        for player in roster[player_type]:
            players.append(
                PlayerInfo(
                    player=Player(
                        name=f"{player['firstName']['default']} {player['lastName']['default']}",
                        id=player["id"],
                        team_id=team.team_id,
                    ),
                    stats=nhl_client.get_player_stats(player["id"]),
                )
            )

    return players


def get_min_max():
    # hardcoding min_max for now
    min_max = {
        "gpg": {"min": 0.0, "max": 2.0},
        "hgpg": {"min": 0.0, "max": 2.0},
        "five_gpg": {"min": 0.0, "max": 2.0},
        "tgpg": {"min": 0.0, "max": 4.0},
        "otga": {"min": 0.0, "max": 4.0},
        "otshga": {"min": 0.0, "max": 1.12},
        "hppg": {"min": 0.0, "max": 0.314},
    }
    return min_max


def make_predictions_teams(players, teams):
    """Score lean players by joining team stats from the relational teams list.

    Hard cutover: ``teams`` is required (one entry per team, keyed by numeric
    ``team_id``). Players carry only skater stats + ``team_id``; team-level
    fields (tgpg/otga/otshga/home) come exclusively from the join. Missing
    teams fail fast so a bad slate surfaces instead of silently scoring.
    """
    teams_by_id = {}
    for team in teams or []:
        if isinstance(team, dict) and team.get("team_id") is not None:
            teams_by_id[team["team_id"]] = team
    if not teams_by_id:
        raise ValueError("make_predictions_teams requires a non-empty relational teams list")

    rust_players = []
    for player in players:
        team = teams_by_id.get(player.get("team_id"))
        if team is None:
            raise KeyError(f"No team entry for player {player.get('name')} (team_id={player.get('team_id')})")
        gpg = player["gpg"]
        hgpg = player["hgpg"]
        five_gpg = player["five_gpg"]
        tgpg = team["tgpg"]
        otga = team["otga"]
        otshga = team["otshga"]
        hppg = player["hppg"]
        is_home = team["home"]
        rust_players.append(
            make_predictions_rust.PlayerInfo(
                gpg=gpg,
                hgpg=hgpg,
                five_gpg=five_gpg,
                tgpg=tgpg,
                otga=otga,
                otshga=otshga,
                hppg=hppg,
                is_home=is_home,
                hppg_otshga=0.0,
            )
        )

    min_max_vals = get_min_max()
    min_max = make_predictions_rust.MinMax(
        min_gpg=min_max_vals["gpg"]["min"],
        max_gpg=min_max_vals["gpg"]["max"],
        min_hgpg=min_max_vals["hgpg"]["min"],
        max_hgpg=min_max_vals["hgpg"]["max"],
        min_five_gpg=min_max_vals["five_gpg"]["min"],
        max_five_gpg=min_max_vals["five_gpg"]["max"],
        min_tgpg=min_max_vals["tgpg"]["min"],
        max_tgpg=min_max_vals["tgpg"]["max"],
        min_otga=min_max_vals["otga"]["min"],
        max_otga=min_max_vals["otga"]["max"],
        min_hppg=min_max_vals["hppg"]["min"],
        max_hppg=min_max_vals["hppg"]["max"],
        min_otshga=min_max_vals["otshga"]["min"],
        max_otshga=min_max_vals["otshga"]["max"],
    )
    rust_probabilities = make_predictions_rust.predict(rust_players, min_max, WEIGHTS)
    for i, player in enumerate(players):
        player["stat"] = rust_probabilities[i]

    return players


def get_tims(players):
    for player in players:
        player["tims"] = 0

    group_ids = get_tims_players()
    if not group_ids:
        return players

    player_table = {player.get("id"): player for player in players}
    for i in range(3):
        for id in group_ids[i]:
            if player_table.get(id):
                player_table[id]["tims"] = i + 1
            else:
                print(f"Player id {id} not found in player list")

    return players


def backfill_dates():
    yesterday = get_date(subtract_days=1)
    dates_no_scored = get_unscored_dates()

    # remove dates that are in the future (shouldn't happen, except maybe today's date)
    dates_no_scored = [date for date in dates_no_scored if date and date <= yesterday]
    logger.info(f"Dates to backfill: {dates_no_scored}")
    if not dates_no_scored:
        return

    scorers_dict = {}
    nhl_client = get_nhl_client()
    for date in dates_no_scored:
        data = nhl_client.get_score(date)

        # get players who actually played
        players = []
        # Only fetched if this date has a postponed game; see resolve_team_names.
        schedule_data = None
        for game in data.get("games"):
            if game.get("gameScheduleState") == "OK":
                if not game.get("gameOutcome"):
                    logger.info(
                        f"Game not completed: {game.get('homeTeam', {}).get('abbrev')} vs {
                            game.get('awayTeam', {}).get('abbrev')
                        }"
                    )
                    return
            if game.get("gameScheduleState") == "PPD":
                # Game was postponed, delete all entries. Unlike the old
                # fire-and-forget Lambda invoke, this is a blocking call, so
                # failures are caught and logged rather than silently dropped.
                if schedule_data is None:
                    schedule_data = fetch_schedule(nhl_client, date)
                team_names = resolve_team_names(
                    schedule_data,
                    [game.get("homeTeam", {}).get("abbrev"), game.get("awayTeam", {}).get("abbrev")],
                )
                try:
                    delete_game_snapshots(date, team_names)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Failed to delete postponed game on {date}: {e}")
                continue

            # Some goals carry no playerId (e.g. unassisted/empty net); drop those
            # rather than stringifying them. Ids stay raw here - backfill_scored
            # coerces them to int and drops non-numerics, since player_id is BIGINT.
            players.extend({goal.get("playerId") for goal in game.get("goals", {}) if goal.get("playerId")})
        scorers_dict[date] = players

    # One request per date, so a window of dozens of dates is dozens of calls.
    # The client retries internally with backoff; run them concurrently to keep
    # the backfill inside the Lambda timeout.
    with ThreadPoolExecutor() as executor:
        futures = {
            executor.submit(backfill_scored, date, player_ids): date for date, player_ids in scorers_dict.items()
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:  # noqa: BLE001
                logger.error(f"Error backfilling scored players for {futures[future]}: {e}")
    return


def publish_public_db(players):
    date = get_date()
    for player in players:
        player["date"] = date
        if not player.get("player_id"):
            player["player_id"] = player.pop("id")

    save_to_db(players)


def check_db_for_date():
    date = get_date()
    logger.info(f"Checking date: {date}")

    entries = get_today_db()
    if entries and entries[0]["date"] == date:
        for entry in entries:
            entry["id"] = entry.pop("player_id")
        return entries
    return None


def merge_players_and_teams(team_payloads):
    """Split team payloads into relational players + teams.

    Team stats stay on a small per-team list (one entry per team, keyed by
    numeric ``team_id``) instead of being duplicated onto every skater.
    Players keep ``team_id`` as the join key plus ``team_name`` for display
    and lineup matching. This keeps Step Functions payloads under the 256KB
    state limit: team stats (~50 bytes/player duplicated) and especially
    goalie stats (~200 bytes/player) are the blowup, not lineups/injuries.

    Returns:
        dict with ``players`` (lean per-skater dicts) and ``teams`` (one
        per-team dict with tgpg/otga/otshga/home + opponent_id).
    """
    players = []
    teams = []
    for team in team_payloads:
        team_info = {
            key: team[key]
            for key in (
                "team_name",
                "team_abbr",
                "season",
                "team_id",
                "opponent_id",
                "home",
                "tgpg",
                "otga",
                "otshga",
            )
            if key in team
        }
        teams.append(team_info)

        for player in team.get("players", []):
            if not isinstance(player, dict):
                continue
            lean = {
                key: player[key]
                for key in ("name", "id", "team_id", "gpg", "hgpg", "five_gpg", "hppg")
                if key in player
            }
            if "team_id" not in lean and "team_id" in team_info:
                lean["team_id"] = team_info["team_id"]
            if "team_name" in team_info:
                lean["team_name"] = team_info["team_name"]
            players.append(lean)

    return {"players": players, "teams": teams}


def choose_picks(players):
    if not players:
        logger.info("No players found, returning empty picks")
        return []
    # get the top pick from each tims {1,2,3}
    tims_picks = {}
    for player in players:
        tims = int(player["tims"])
        if tims not in tims_picks:
            tims_picks[tims] = player
        elif player["stat"] > tims_picks[tims]["stat"]:
            tims_picks[tims] = player
    tims_picks.pop(0, None)

    if len(tims_picks) < NUM_EXPECTED_PLAYERS:
        logger.error(f"Less than {NUM_EXPECTED_PLAYERS} tims picks found: {tims_picks.keys()}")
        return []

    for i in range(1, NUM_EXPECTED_PLAYERS + 1):
        tims_picks[i]["Scored"] = None
    return list(tims_picks.values())


def write_historic_db(picks):
    today = get_date()
    if picks:
        for player in picks:
            player["date"] = today
            player["player_id"] = player.pop("id")

    old_entries = get_historical_data()
    table = defaultdict(list)
    for entry in old_entries:
        table[entry["date"]].append((entry["player_id"], entry["Scored"]))

    if today in table.keys():
        logger.info(f"Today already in table: {table[today]}")
        # Already processed today (e.g. a retry or a second run). Returning []
        # keeps the cumulative metrics idempotent instead of recounting.
        return []

    if picks:
        while len(table) >= DAYS_TO_KEEP_HISTORIC_DATA:
            last_date = min(table.keys())
            table.pop(last_date)
        old_entries = [entry for entry in old_entries if entry["date"] in table.keys()]

    dates_no_scored = [
        date for date in table.keys() if date and any(scored is None for _, scored in table[date]) and date < today
    ]
    logger.info(f"Updating scored column for dates: {dates_no_scored}")
    for date in dates_no_scored:
        players = get_players_for_date(date)

        # Archive rows key on player_id (the NHL id). Mongo's documents carried
        # it in `id`, so this used to be {player["id"]: player}.
        player_table = {player["player_id"]: player for player in players}

        for entry in old_entries:
            if entry["date"] == date:
                player = player_table.get(entry["player_id"])
                # `scored` is nullable: null means the game has not been graded
                # yet. Only copy a real value across, otherwise this would
                # overwrite a pick that a previous date's backfill already
                # graded, just because the date still has *some* unscored pick.
                if player and player.get("scored") is not None:
                    entry["Scored"] = int(player["scored"])

    data = old_entries + picks if picks else old_entries
    update_historical_data(data)

    return _collect_newly_scored_entries(old_entries, dates_no_scored)


def _collect_newly_scored_entries(old_entries, dates_no_scored):
    """Collect entries for newly-resolved dates so metrics count all of them.

    Previously only yesterday's 3 players were returned, so when the next day
    had no games (no entries, e.g. 2026-10-03) the day before it (2026-10-02)
    was never counted and the season/lifetime totals stayed short by 3
    forever. A date counts only when it has a complete slate
    (NUM_EXPECTED_PLAYERS entries, all scored); incomplete dates are left for
    a later run.
    """
    newly_scored = []
    for date in dates_no_scored:
        entries = [e for e in old_entries if e.get("date") == date]
        if len(entries) == NUM_EXPECTED_PLAYERS and all(e.get("Scored") is not None for e in entries):
            newly_scored.extend(entries)
        else:
            logger.warning(
                f"Skipping metrics for date {date}: expected {NUM_EXPECTED_PLAYERS} scored entries, "
                f"found {len(entries)} with {[e.get('Scored') for e in entries]}"
            )
    return newly_scored


ROTOWIRE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}

ROTOWIRE_GOALIE_TABLE_URL = "https://www.rotowire.com/hockey/tables/projected-goalies.php"

# RotoWire team abbreviations that differ from the official NHL API abbreviations.
ROTOWIRE_TEAM_ABBR_MAP = {
    "MON": "MTL",
    "LAS": "VGK",
}


def normalize_rotowire_team_abbr(abbr: str | None) -> str:
    """Normalize a RotoWire team abbreviation to the official NHL API abbreviation."""
    if not abbr:
        return ""
    return ROTOWIRE_TEAM_ABBR_MAP.get(abbr.upper(), abbr.upper())


def get_injury_data() -> List[Dict[str, str]]:
    """
    Get current injury data from RotoWire.

    Returns:
        List of injury dictionaries with keys:
        - player: Name of the injured player
        - injury: Injury description
        - status: Injury status
    """
    url = "https://www.rotowire.com/hockey/tables/injury-report.php?team=ALL&pos=ALL"

    try:
        response = requests.get(url, headers=ROTOWIRE_HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        logger.error(f"Error fetching injury data: {e}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing injury JSON: {e}")
        return []

    injuries = []
    for item in data:
        try:
            player = item.get("player", "")
            injury = item.get("injury", "")
            status = item.get("status", "")

            # Only include if we have at least player name and injury info
            if player and (injury or status):
                injuries.append(
                    {
                        "player": player,
                        "injury": injury,
                        "status": status,
                    }
                )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error extracting injury data: {e}")
            continue

    logger.info(f"Scraped {len(injuries)} injury updates")
    return injuries


def merge_injury_data(players: List[Dict], injuries: List[Dict[str, str]]) -> List[Dict]:
    """
    Merge injury data into the player list.

    Args:
        players: List of player dictionaries
        injuries: List of injury dictionaries from RotoWire

    Returns:
        List of players with added injury information
    """
    injury_dict = {injury["player"].lower(): injury for injury in injuries}

    for player in players:
        player_name = player.get("name", "").lower()
        if player_name in injury_dict:
            injury = injury_dict[player_name]
            player["injury_status"] = "INJURED"
            player["injury_desc"] = injury["status"]
        else:
            player["injury_status"] = "HEALTHY"
            player["injury_desc"] = ""

    return players


def get_starting_goalies(date: str | None = None) -> List[Dict]:
    """
    Get projected/confirmed starting goalies for a date from RotoWire.

    Uses the same tables JSON pattern as the injury report
    (`/hockey/tables/projected-goalies.php?date=YYYY-MM-DD`).

    Args:
        date: Date in YYYY-MM-DD format. Defaults to today (Toronto time).

    Returns:
        List of starter dictionaries with keys:
        - date, team_abbr (NHL-normalized), home (bool),
          goalie_name, rotowire_id, status (e.g. Confirmed/Expected/Unknown)
    """
    date = date or get_date()
    url = f"{ROTOWIRE_GOALIE_TABLE_URL}?date={date}"

    try:
        data = exponential_backoff_request(url, headers=ROTOWIRE_HEADERS)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error fetching starting goalie data: {e}")
        return []

    if not isinstance(data, list):
        logger.error(f"Unexpected starting goalie payload type: {type(data)}")
        return []

    starters = []
    for game in data:
        if not isinstance(game, dict):
            continue
        for side, home in (("home", True), ("visit", False)):
            try:
                name = (game.get(f"{side}Player") or "").strip()
                team = normalize_rotowire_team_abbr(game.get(f"{side}team", ""))
                status = (game.get(f"{side}Status") or "").strip()
                if not name or not team:
                    continue
                starters.append(
                    {
                        "date": date,
                        "team_abbr": team,
                        "home": home,
                        "goalie_name": name,
                        "rotowire_id": game.get(f"{side}PlayerID"),
                        "status": status or "Unknown",
                    }
                )
            except Exception as e:  # noqa: BLE001
                logger.error(f"Error extracting starting goalie data: {e}")
                continue

    logger.info(f"Scraped {len(starters)} starting goalies for {date}")
    return starters


def _parse_goalie_stats(data: object, team_abbr: str) -> Dict[str, Dict]:
    """
    Parse a club-stats payload into a mapping of lowercase goalie name to stats.

    Args:
        data: Decoded JSON body from the club-stats endpoint.
        team_abbr: Team abbreviation, used only for log messages.

    Returns:
        Mapping of lowercase goalie name to stats dict. Empty if the payload has
        no goalies, which is expected before a season starts.
    """
    if not isinstance(data, dict):
        logger.error(f"Unexpected goalie stats payload type for {team_abbr}: {type(data)}")
        return {}

    stats = {}
    for goalie in data.get("goalies") or []:
        try:
            if not isinstance(goalie, dict):
                continue
            first = ((goalie.get("firstName") or {}).get("default") or "").strip()
            last = ((goalie.get("lastName") or {}).get("default") or "").strip()
            name = f"{first} {last}".strip()
            if not name:
                continue
            wins = goalie.get("wins", 0) or 0
            losses = goalie.get("losses", 0) or 0
            ot_losses = goalie.get("overtimeLosses", 0) or 0
            stats[name.lower()] = {
                "nhl_id": goalie.get("playerId"),
                "gaa": goalie.get("goalsAgainstAverage"),
                "save_pct": goalie.get("savePercentage"),
                "wins": wins,
                "losses": losses,
                "ot_losses": ot_losses,
                "record": f"{wins}-{losses}-{ot_losses}",
                "shutouts": goalie.get("shutouts", 0) or 0,
                "games_played": goalie.get("gamesPlayed", 0) or 0,
                "games_started": goalie.get("gamesStarted", 0) or 0,
            }
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error extracting goalie stats for {team_abbr}: {e}")
            continue

    return stats


def get_goalie_stats_for_team(team_abbr: str) -> Dict[str, Dict]:
    """
    Get current season stats for all goalies on a team from the official NHL API.

    The ``/now`` endpoint reports the in-progress season only. Before a season
    starts it legitimately returns no goalies (e.g. September, when the new
    season is 20262027 but no regular-season games have been played), and stats
    are left null rather than backfilled from a prior season -- a goalie with no
    games played this season genuinely has no current-season stats.

    Args:
        team_abbr: Official NHL team abbreviation (e.g. TOR).

    Returns:
        Mapping of lowercase goalie name to stats dict with keys:
        nhl_id, gaa, save_pct, wins, losses, ot_losses, record,
        shutouts, games_played, games_started. Empty before a season starts.
    """
    url = f"https://api-web.nhle.com/v1/club-stats/{team_abbr}/now"

    try:
        data = exponential_backoff_request(url)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error fetching goalie stats for {team_abbr}: {e}")
        return {}

    stats = _parse_goalie_stats(data, team_abbr)
    if not stats and isinstance(data, dict):
        # Not an error: /now has no rows until the team plays a regular-season game.
        logger.info(f"No current-season goalie stats for {team_abbr} (season {data.get('season')} has no games yet)")

    return stats


def enrich_starting_goalies(date: str | None = None) -> List[Dict]:
    """
    Get starting goalies for a date enriched with official NHL stats.

    Club stats are fetched once per team.

    Args:
        date: Date in YYYY-MM-DD format. Defaults to today (Toronto time).

    Returns:
        List of starter dictionaries including gaa, save_pct, record, etc.
    """
    starters = get_starting_goalies(date)
    teams = sorted({starter["team_abbr"] for starter in starters})
    stats_by_team = {team: get_goalie_stats_for_team(team) for team in teams}

    enriched = []
    for starter in starters:
        info = dict(starter)
        team_stats = stats_by_team.get(starter["team_abbr"], {})
        stat = team_stats.get(starter["goalie_name"].lower(), {})
        if not stat and team_stats:
            logger.warning(
                f"Goalie '{starter['goalie_name']}' ({starter['team_abbr']}) not found in NHL club stats "
                f"(have: {sorted(team_stats)})"
            )
        info.update(
            {
                "nhl_id": stat.get("nhl_id"),
                "gaa": stat.get("gaa"),
                "save_pct": stat.get("save_pct"),
                "wins": stat.get("wins"),
                "losses": stat.get("losses"),
                "ot_losses": stat.get("ot_losses"),
                "record": stat.get("record"),
                "shutouts": stat.get("shutouts"),
                "games_played": stat.get("games_played"),
                "games_started": stat.get("games_started"),
            }
        )
        enriched.append(info)

    return enriched


def _team_display_name(team):
    """The NHL schedule place name for a team, e.g. "Toronto".

    ``placeName`` is a single space for some entries, which is why ``get_teams``
    falls back to ``commonName``. Every consumer of ``team_name`` has to agree
    on this, since the value is the join key between the roster payload and the
    schedule.
    """
    place = ((team.get("placeName") or {}).get("default") or "").strip()
    if place and place != " ":
        return place
    return ((team.get("commonName") or {}).get("default") or "").strip()


def build_team_name_map(schedule_data: Dict) -> Dict[str, str]:
    """
    Map NHL team display name to abbreviation using the same logic as get_teams.

    Args:
        schedule_data: Raw response from the NHL schedule endpoint.

    Returns:
        Mapping of team display name to team abbreviation.
    """
    mapping = {}
    try:
        games = schedule_data.get("gameWeek", [])[0].get("games", [])
    except (AttributeError, IndexError, KeyError, TypeError):
        logger.error("Unexpected schedule payload when building team name map")
        return {}

    for game in games:
        for side in ("homeTeam", "awayTeam"):
            team = game.get(side, {})
            name = _team_display_name(team)
            abbr = team.get("abbrev", "")
            if not name or not abbr:
                continue
            if name in mapping and mapping[name] != abbr:
                logger.warning(f"Ambiguous team name in schedule: {name}")
            mapping[name] = abbr

    return mapping


def fetch_schedule(nhl_client, date):
    """Fetch a date's NHL schedule, returning ``{}`` if the fetch fails.

    A schedule lookup is only needed on the postponed-game path, so a failure
    here must not take down the rest of the backfill.
    """
    try:
        return nhl_client.get_schedule(date)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Could not fetch the NHL schedule for {date}: {e}")
        return {}


def resolve_team_names(schedule_data: Dict, team_abbrs) -> List[str]:
    """Translate team abbreviations into the ``team_name`` snapshots are keyed on.

    ``Player-Snapshots`` stores ``team_name`` (the schedule place name, e.g.
    "Toronto"), but ``team_abbr`` is stripped from the pipeline payload by
    ``TEAM_MERGE_EXCLUDED_FIELDS`` and the NHL score feed only reports
    abbreviations. The postponed-game delete therefore has to go through the
    schedule, which is the same payload ``build_team_name_map`` already reads.
    Inverts that map so the two stay in step.

    Abbreviations the schedule does not know are dropped (and logged) rather
    than guessed at, because a wrong name would delete another team's rows.
    """
    abbr_to_name = {abbr: name for name, abbr in build_team_name_map(schedule_data).items()}

    names = []
    missing = []
    for abbr in team_abbrs or []:
        if not abbr:
            continue
        name = abbr_to_name.get(abbr)
        if name:
            names.append(name)
        else:
            missing.append(abbr)

    if missing:
        logger.warning(f"Could not resolve team abbreviation(s) to a snapshot team_name: {sorted(set(missing))}")

    return names


def build_goalies_with_team_id(starters: List[Dict], teams: List[Dict] | None) -> List[Dict]:
    """Attach numeric ``team_id`` to enriched starters for relational joins.

    RotoWire reports starters by (normalized) abbreviation while players join
    on numeric ``team_id``. The ``teams`` list from ParseData carries both,
    so this resolves abbr -> team_id once instead of duplicating the full
    goalie stat block onto every skater (~200 bytes/player, the payload
    that pushed GetGoalies over the 256KB Step Functions limit).

    Starters whose abbreviation is not on today's slate keep no ``team_id``
    and are ignored by the DB join rather than failing the step.
    """
    abbr_to_id = {}
    for team in teams or []:
        if not isinstance(team, dict):
            continue
        abbr = (team.get("team_abbr") or "").upper()
        team_id = team.get("team_id")
        if abbr and team_id is not None:
            abbr_to_id[abbr] = team_id

    goalies = []
    for starter in starters:
        if not isinstance(starter, dict):
            continue
        goalie = dict(starter)
        team_id = abbr_to_id.get((starter.get("team_abbr") or "").upper())
        if team_id is not None:
            goalie["team_id"] = team_id
        else:
            logger.warning(
                f"Starting goalie '{starter.get('goalie_name')}' ({starter.get('team_abbr')}) "
                "not on today's slate; skipping team join"
            )
        goalies.append(goalie)

    return goalies


def denormalize_players_for_db(
    players: List[Dict],
    teams: List[Dict] | None = None,
    goalies: List[Dict] | None = None,
) -> List[Dict]:
    """Join relational players + teams + goalies into legacy full rows.

    Used only inside the final DB lambdas (SaveToDb / UpdateHistory /
    PublishDb) so Supabase/Mongo keep the exact denormalized columns they
    have today (team stats + ``opp_goalie_*`` per skater). The Step Functions
    state itself stays lean; this never mutates its inputs.

    Join keys (``team_id`` and friends) are stripped so rows match the legacy
    schema -- Supabase rejects upserts with unknown columns, and the Picks /
    Historic-Picks tables have no ``team_id`` column.
    """
    if not teams and not goalies:
        return players

    teams_by_id = {
        team["team_id"]: team for team in (teams or []) if isinstance(team, dict) and team.get("team_id") is not None
    }
    goalies_by_team: Dict[int, Dict] = {}
    for goalie in goalies or []:
        if not isinstance(goalie, dict) or goalie.get("team_id") is None:
            continue
        team_id = goalie["team_id"]
        if team_id in goalies_by_team:
            logger.warning(f"Multiple starters listed for team_id {team_id}, keeping the last one")
        goalies_by_team[team_id] = goalie

    full_rows = []
    for player in players:
        row = dict(player)
        team_id = player.get("team_id")
        team = teams_by_id.get(team_id, {}) if team_id is not None else {}
        if team:
            for key in ("team_name", "home", "tgpg", "otga", "otshga"):
                if key in team:
                    row[key] = team[key]
        # Strip relational-only join keys so the row matches the legacy
        # denormalized DB shape (no team_id / opponent_id / season / team_abbr).
        for join_key in ("team_id", "opponent_id", "season", "team_abbr"):
            row.pop(join_key, None)
        opp = {}
        if team:
            opp_id = team.get("opponent_id")
            opp = goalies_by_team.get(opp_id, {}) if opp_id is not None else {}
        if opp or teams is not None:
            row["opp_goalie_name"] = opp.get("goalie_name")
            row["opp_goalie_team"] = opp.get("team_abbr")
            row["opp_goalie_status"] = (opp.get("status") or "UNKNOWN").upper() if opp else "UNKNOWN"
            row["opp_goalie_confirmed"] = bool(opp) and (opp.get("status") or "").lower() == "confirmed"
            row["opp_goalie_nhl_id"] = opp.get("nhl_id")
            row["opp_goalie_gaa"] = opp.get("gaa")
            row["opp_goalie_save_pct"] = opp.get("save_pct")
            row["opp_goalie_record"] = opp.get("record")
            row["opp_goalie_shutouts"] = opp.get("shutouts")
            row["opp_goalie_games_played"] = opp.get("games_played")
        full_rows.append(row)

    return full_rows


def _iter_teams(games: List[Dict]) -> List[Dict]:
    """Yield team dicts from a list of games, skipping anything malformed.

    Source payloads are external data, so a shape change should degrade to fewer
    matched players rather than raise and take down the pipeline step.
    """
    teams = []
    if not isinstance(games, list):
        return teams
    for game in games:
        if not isinstance(game, dict):
            continue
        game_teams = game.get("teams")
        if not isinstance(game_teams, list):
            continue
        teams.extend(team for team in game_teams if isinstance(team, dict))
    return teams


def _build_lineup_lookup(nhl_games: List[Dict], rotowire_games: List[Dict]) -> Dict[str, Dict[str, Dict]]:
    """
    Build ``team name -> {normalized player name: unit info}`` from both sources.

    Even-strength units (F1-F4 forward lines, D1-D3 pairs, G1/G2 goalies) come from
    the NHL.com projections article; power play units come from RotoWire, which
    publishes them where NHL.com does not. The two are kept as separate keys so a
    player can carry both, which is the normal case for a top-six forward.

    Team names are keyed exactly as the sources report them ("Blue Jackets"), and
    players are matched on ``team_name`` from the same NHL schedule, so no
    cross-source team-name translation is needed.

    Returns:
        Mapping of team name to a mapping of normalized player name to
        ``{"unit": str|None, "pp_unit": str|None}``.
    """
    lookup: Dict[str, Dict[str, Dict]] = {}

    def team_entry(team_name: str) -> Dict[str, Dict]:
        return lookup.setdefault(team_name, {})

    def assign_unit(team_name: str, player_name: str, label: str) -> None:
        key = normalize_player_name(player_name)
        if not key:
            return
        entries = team_entry(team_name)
        existing = entries.setdefault(key, {}).get("unit")
        if existing and existing != label:
            # The article occasionally lists a player on two lines at once. The
            # lookup is name-keyed so only one can survive; say so rather than
            # letting it look like a clean parse.
            logger.warning(f"{team_name}: {player_name} listed on both {existing} and {label}; keeping {existing}")
            return
        entries[key]["unit"] = label

    def assign_pp_unit(team_name: str, player_name: str, label: str) -> None:
        key = normalize_player_name(player_name)
        if key:
            team_entry(team_name).setdefault(key, {})["pp_unit"] = label

    for team in _iter_teams(nhl_games):
        team_name = team.get("name") or ""
        if not team_name:
            continue
        for unit in team.get("units") or []:
            if not isinstance(unit, dict):
                continue
            for player_name in unit.get("players") or []:
                assign_unit(team_name, player_name, unit.get("label") or "")

    for team in _iter_teams(rotowire_games):
        team_name = team.get("name") or ""
        if not team_name:
            continue
        for unit in team.get("pp_units") or []:
            if not isinstance(unit, dict):
                continue
            for player in unit.get("players") or []:
                if isinstance(player, dict):
                    assign_pp_unit(team_name, player.get("name") or "", unit.get("label") or "")

    return lookup


def mark_lineups_unknown(players: List[Dict]) -> List[Dict]:
    """
    Stamp lineup fields as UNKNOWN without consulting any source.

    Used when the lineup fetch or merge fails outright, so the day's rows still
    carry the lineup columns (as UNKNOWN) rather than missing them entirely.
    """
    for player in players:
        player["lineup_unit"] = None
        player["lineup_position_group"] = None
        player["pp_unit"] = None
        player["lineup_status"] = "UNKNOWN"
    return players


def merge_lineup_data(
    players: List[Dict],
    nhl_games: List[Dict],
    rotowire_games: List[Dict],
) -> List[Dict]:
    """
    Annotate each player with their projected starting lineup units.

    Args:
        players: List of player dictionaries (must include name and team_name).
        nhl_games: Games from get_nhl_com_lineups.
        rotowire_games: Games from get_rotowire_lineups.

    Returns:
        List of players with added ``lineup_unit``, ``lineup_position_group``,
        ``pp_unit`` and ``lineup_status`` fields.

        ``lineup_unit`` is only set for forwards (F1-F4); defence pairings and
        goalie designations are parsed and available on the source payload but not
        stored per skater, since the picks table is skater-scoped. ``lineup_status``
        is PROJECTED when a forward line matched and UNKNOWN otherwise, so an empty
        fetch is distinguishable from a genuine miss.
    """
    lookup = _build_lineup_lookup(nhl_games, rotowire_games)

    matched = total = 0
    for player in players:
        entries = lookup.get(player.get("team_name", ""), {})
        entry = entries.get(normalize_player_name(player.get("name", "")), {})

        unit = entry.get("unit") or ""
        is_forward = unit.startswith("F")
        pp_unit = entry.get("pp_unit")

        player["lineup_unit"] = unit if is_forward else None
        player["lineup_position_group"] = unit[:1] if unit else None
        player["pp_unit"] = pp_unit
        player["lineup_status"] = "PROJECTED" if is_forward else "UNKNOWN"

        total += 1
        matched += bool(is_forward)

    logger.info(f"Matched {matched}/{total} players to a projected forward line")
    if total and matched < total:
        logger.warning(
            f"Only {matched}/{total} players matched a projected line; check that the "
            "NHL.com article and the player list refer to the same slate of games"
        )

    return players


def calculate_metrics(yesterday_results: List[Dict]) -> List[Dict]:
    # Accepts one or more complete days (multiples of NUM_EXPECTED_PLAYERS) so
    # catch-up runs after an outage or a no-game day count every newly-resolved
    # date instead of dropping all but 3 players.
    if not yesterday_results or len(yesterday_results) % NUM_EXPECTED_PLAYERS != 0:
        logger.warning(
            f"Results do not have a complete slate (multiple of {NUM_EXPECTED_PLAYERS} players), "
            "skipping metrics calculation"
        )
        return []

    cur_picks_overall = get_cur_pick_pct()
    if not cur_picks_overall:
        return {
            "value": "-",
            "correct": "-",
            "total": "-",
        }

    correct_picks = sum(1 for player in yesterday_results if player.get("Scored") == 1)
    new_total = cur_picks_overall["total"] + len(yesterday_results)
    new_correct = cur_picks_overall["correct"] + correct_picks

    return {
        "value": round((new_correct / new_total) * 100, 2),
        "total": new_total,
        "correct": new_correct,
    }


def update_metrics(new_metrics: List[Dict]) -> None:
    if not new_metrics:
        logger.warning("No new metrics to update")
        return

    upload_metrics(new_metrics)


def resolve_season_id(yesterday_results=None, fallback_date=None):
    """Resolve NHL season id for yesterday's results.

    Prefers the date on the result rows so a season boundary doesn't
    misattribute old-season results to the new season row.
    """
    result_date = None
    if yesterday_results:
        for player in yesterday_results:
            if player.get("date"):
                result_date = player.get("date")
                break
    if result_date:
        return get_season_id(result_date)
    if fallback_date:
        return get_season_id(fallback_date)
    return get_season_id(get_date(subtract_days=1))


def calculate_season_metrics(yesterday_results: List[Dict], season_id=None) -> List[Dict]:
    """Season-scoped cumulative accuracy, parallel to lifetime calculate_metrics.

    Lifetime flow is left untouched. When no season row exists yet (new season),
    initializes from the newly-resolved results instead of returning "-"
    placeholders. Accepts one or more complete days (multiples of
    NUM_EXPECTED_PLAYERS) so catch-up runs count every date.
    """
    if not yesterday_results or len(yesterday_results) % NUM_EXPECTED_PLAYERS != 0:
        logger.warning(
            f"Results do not have a complete slate (multiple of {NUM_EXPECTED_PLAYERS} players), "
            "skipping season metrics"
        )
        return []

    if season_id is None:
        season_id = resolve_season_id(yesterday_results)

    cur_season = get_season_pick_pct(season_id)
    correct_picks = sum(1 for player in yesterday_results if player.get("Scored") == 1)

    if not cur_season:
        new_total = len(yesterday_results)
        new_correct = correct_picks
    else:
        new_total = cur_season["total"] + len(yesterday_results)
        new_correct = cur_season["correct"] + correct_picks

    return {
        "value": round((new_correct / new_total) * 100, 2) if new_total else 0.0,
        "total": new_total,
        "correct": new_correct,
    }


def update_season_metrics(new_metrics: List[Dict], season_id) -> None:
    if not new_metrics:
        logger.warning("No new season metrics to update")
        return
    if not season_id:
        logger.warning("No season_id for season metrics, skipping")
        return

    upload_season_metrics(new_metrics, season_id)


def get_all_emails() -> List[str]:
    return get_emails()


def send_emails(users: List[str], picks: List[Dict]) -> None:
    if not is_feature_enabled("send_emails"):
        logger.info("Feature flag disabled: skipping email sends")
        return

    with ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(send_email, user["email"], picks, user.get("display_name", ""), get_date())
            for user in users
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:  # noqa: BLE001
                logger.error(f"Error sending email in parallel: {e}")
