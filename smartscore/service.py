import datetime
import json
from collections import defaultdict

import make_predictions_rust
import pytz
from aws_lambda_powertools import Logger
from smartscore_info_client.schemas.player_info import PLAYER_INFO_SCHEMA, PlayerInfo
from smartscore_info_client.schemas.team_info import TEAM_INFO_SCHEMA, TeamInfo

from config import ENV
from constants import DAYS_TO_KEEP_HISTORIC_DATA, LAMBDA_API_NAME
from utility import (
    exponential_backoff_request,
    get_historical_data,
    get_tims_players,
    get_today_db,
    invoke_lambda,
    remove_last_game,
    save_to_db,
    schedule_run,
    update_historical_data,
)

logger = Logger()


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

    URL = f"https://api-web.nhle.com/v1/schedule/{date}"
    return exponential_backoff_request(URL)


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

        home_team = TeamInfo(
            team_name=home_name,
            team_abbr=game["homeTeam"]["abbrev"],
            season=game["season"],
            team_id=game["homeTeam"]["id"],
            opponent_id=game["awayTeam"]["id"],
            home=True,
        )
        away_team = TeamInfo(
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
        start_times = remove_last_game(start_times)
        schedule_run(start_times)

    return teams


def get_players_from_team(team):
    players = []

    URL = f"https://api-web.nhle.com/v1/roster/{team.team_abbr}/current"
    data = exponential_backoff_request(URL)

    types = ["forwards", "defensemen"]
    for player_type in types:
        for player in data[player_type]:
            player_info = PlayerInfo(
                name=f"{player["firstName"]["default"]} {player["lastName"]["default"]}",
                id=player["id"],
                team_id=team.team_id,
            )
            players.append(player_info)

    return players


def get_min_max():
    # payload = {
    #     "method": "GET_MIN_MAX",
    # }
    # data = invoke_lambda("Api", payload)
    # min_max = data.get("body", {})

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


def make_predictions_teams(players):
    rust_players = []
    for player in players:
        rust_players.append(
            make_predictions_rust.PlayerInfo(
                gpg=player["gpg"],
                hgpg=player["hgpg"],
                five_gpg=player["five_gpg"],
                tgpg=player["tgpg"],
                otga=player["otga"],
                otshga=player["otshga"],
                hppg=player["hppg"],
                is_home=player["home"],
                hppg_otshga=0.0,
            )
        )

    # experimental weights
    weights = {
        "gpg": 0.6,
        "five_gpg": 0.06,
        "hgpg": 0.0,
        "tgpg": 0.02,
        "otga": 0.16,
        "hppg_otshga": 0.02,
        "is_home": 0.14,
    }

    # rust weights
    weights = make_predictions_rust.Weights(
        gpg=0.3,
        five_gpg=0.4,
        hgpg=0.3,
        tgpg=0.0,
        otga=0.0,
        hppg_otshga=0.0,
        is_home=0.0,
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
    rust_probabilities = make_predictions_rust.predict(rust_players, min_max, weights)
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
    response = invoke_lambda(f"Api-{ENV}", {"method": "GET_DATES_NO_SCORED"})
    body = response.get("body", {})
    dates_no_scored = json.loads(body.get("dates", "[]"))

    # remove dates that are in the future (shouldn't happen, except maybe today's date)
    dates_no_scored = [date for date in dates_no_scored if date and date <= yesterday]
    logger.info(f"Dates to backfill: {dates_no_scored}")
    if not dates_no_scored:
        return

    scorers_dict = {}
    for date in dates_no_scored:
        data = exponential_backoff_request(f"https://api-web.nhle.com/v1/score/{date}")

        # get players who actually played
        players = []
        for game in data.get("games"):
            if game.get("gameScheduleState") == "OK":
                if not game.get("gameOutcome"):
                    logger.info(
                        f"Game not completed: {
                        game.get('homeTeam', {}).get('abbrev')
                    } vs {
                        game.get('awayTeam', {}).get('abbrev')
                    }"
                    )
                    return
            if game.get("gameScheduleState") == "PPD":
                # Game was postponed, delete all entries
                invoke_lambda(
                    function_name=LAMBDA_API_NAME,
                    payload={
                        "method": "DELETE_GAME",
                        "date": date,
                        "home": game.get("homeTeam", {}).get("abbrev"),
                        "away": game.get("awayTeam", {}).get("abbrev"),
                    },
                    wait=False,
                )
                continue

            players.extend(list({goal.get("playerId") for goal in game.get("goals", {})}))
        scorers_dict[date] = players

    response = invoke_lambda(LAMBDA_API_NAME, {"method": "POST_BACKFILL", "data": scorers_dict})
    return response


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


def separate_players(players, teams):
    entries = []
    team_table = {team.team_id: TEAM_INFO_SCHEMA.dump(team) for team in teams}
    for player in players:
        team_info = team_table[player.team_id]
        team_info_filtered = {
            key: value
            for key, value in team_info.items()
            if key not in ("team_id", "opponent_id", "season", "team_abbr")
        }

        player_data = PLAYER_INFO_SCHEMA.dump(player)
        player_info_filtered = {
            key: value for key, value in player_data.items() if key not in ("team_id", "odds", "stat")
        }

        entries.append({**player_info_filtered, **team_info_filtered})

    return entries


def choose_picks(players):
    if not players:
        logger.info("No players found, returning empty picks")
        return []
    # get the top pick from each tims {1,2,3}
    tims_picks = {}
    for player in players:
        tims = player["tims"]
        if tims not in tims_picks:
            tims_picks[tims] = player
        elif player["stat"] > tims_picks[tims]["stat"]:
            tims_picks[tims] = player
    tims_picks.pop(0, None)

    for i in range(1, 4):
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
        return

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
        response = invoke_lambda(f"Api-{ENV}", {"method": "GET_DATE", "date": date})
        body = response.get("body", "[]")
        players = json.loads(body)

        player_table = {player["id"]: player for player in players}

        for entry in old_entries:
            if entry["date"] == date:
                player = player_table.get(entry["player_id"])
                if player:
                    entry["Scored"] = int(player["scored"])

    data = old_entries + picks if picks else old_entries
    update_historical_data(data)
    return
