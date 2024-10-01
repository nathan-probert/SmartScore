import datetime

import requests
from aws_lambda_powertools import Logger

from schemas.player_info import PlayerInfo
from schemas.team_info import TeamInfo

logger = Logger()


def get_todays_schedule():
    date = datetime.date.today().strftime("%Y-%m-%d")
    logger.info(f"Getting players for date: {date}")

    URL = f"https://api-web.nhle.com/v1/schedule/{date}"
    return requests.get(URL, timeout=10).json()


def get_teams(data):
    games = data["gameWeek"][0]["games"]

    teams = []
    for game in games:
        home_team = TeamInfo(
            name=game["homeTeam"]["placeName"]["default"],
            abbr=game["homeTeam"]["abbrev"],
            season=game["season"],
            id=game["homeTeam"]["id"],
            opponent_id=game["awayTeam"]["id"],
        )
        away_team = TeamInfo(
            name=game["awayTeam"]["placeName"]["default"],
            abbr=game["awayTeam"]["abbrev"],
            season=game["season"],
            id=game["awayTeam"]["id"],
            opponent_id=game["homeTeam"]["id"],
        )

        teams.append(home_team)
        teams.append(away_team)

    return teams


def get_players_from_team(teams):
    players = []

    for team in teams:
        logger.info(f"Getting players for team: {team.name}")
        URL = f"https://api-web.nhle.com/v1/roster/{team.abbr}/current"
        data = requests.get(URL, timeout=10).json()

        types = ["forwards", "defensemen"]
        for player_type in types:
            for player in data[player_type]:
                player_info = PlayerInfo(
                    name=player["firstName"]["default"] + " " + player["lastName"]["default"],
                    id=player["id"],
                )
                players.append(player_info)

    return players
