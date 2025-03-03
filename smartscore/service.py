import datetime
import json

import pytz
import requests
from aws_lambda_powertools import Logger
from smartscore_info_client.schemas.player_info import PLAYER_INFO_SCHEMA, PlayerInfo
from smartscore_info_client.schemas.team_info import TEAM_INFO_SCHEMA, TeamInfo

from config import ENV
from constants import LAMBDA_API_NAME
from utility import (
    c_predict,
    get_tims_players,
    get_today_db,
    invoke_lambda,
    remove_last_game,
    save_to_db,
    schedule_run,
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
    return requests.get(URL, timeout=5).json()


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

    start_times = remove_last_game(start_times)
    if not start_times:
        logger.info("No start times found")
    else:
        schedule_run(start_times)

    return teams


def get_players_from_team(team):
    players = []

    URL = f"https://api-web.nhle.com/v1/roster/{team.team_abbr}/current"
    data = requests.get(URL, timeout=10).json()

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
    min_max = get_min_max()
    c_players = []
    for player in players:
        c_players.append(
            {
                "gpg": player["gpg"],
                "hgpg": player["hgpg"],
                "five_gpg": player["five_gpg"],
                "tgpg": player["tgpg"],
                "otga": player["otga"],
                "otshga": player["otshga"],
                "hppg": player["hppg"],
                "is_home": player["is_home"],
            }
        )

    probabilities = c_predict(c_players, min_max)
    for i, player in enumerate(players):
        player["stat"] = probabilities[i]

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
    today = get_date(subtract_days=1)
    logger.info(f"Checking for existence of date: {today}")
    response = invoke_lambda(f"Api-{ENV}", {"method": "GET_DATES_NO_SCORED"})
    body = response.get("body", {})
    dates_no_scored = json.loads(body.get("dates", "[]"))

    # remove dates that are in the future (shouldn't happen, except maybe today's date)
    dates_no_scored = [date for date in dates_no_scored if date < today]
    logger.info(f"Dates to backfill: {dates_no_scored}")
    if not dates_no_scored:
        return

    scorers_dict = {}
    for date in dates_no_scored:
        data = requests.get(f"https://api-web.nhle.com/v1/score/{date}", timeout=5).json()

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
