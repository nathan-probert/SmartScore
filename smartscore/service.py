import datetime
import random
import time

import pytz
import requests
from aws_lambda_powertools import Logger
from smartscore_info_client.schemas.player_info import PlayerInfo
from smartscore_info_client.schemas.team_info import TeamInfo

from constants import DRAFTKINGS_GOAL_SCORER_CATEGORY, DRAFTKINGS_NHL_ID
from smartscore_info_client.schemas.team_info import TEAM_INFO_SCHEMA
from smartscore_info_client.schemas.player_info import PLAYER_INFO_SCHEMA
from utility import c_predict, get_tims_players, invoke_lambda, link_odds, save_to_db
import json

logger = Logger()


def get_date():
    toronto_tz = pytz.timezone("America/Toronto")
    return datetime.datetime.now(toronto_tz).strftime("%Y-%m-%d")


def get_todays_schedule():
    date = get_date()
    logger.info(f"Getting players for date: {date}")

    URL = f"https://api-web.nhle.com/v1/schedule/{date}"
    return requests.get(URL, timeout=5).json()


def get_teams(data):
    games = data["gameWeek"][0]["games"]

    teams = []
    for game in games:
        home_team = TeamInfo(
            team_name=game["homeTeam"]["placeName"]["default"],
            team_abbr=game["homeTeam"]["abbrev"],
            season=game["season"],
            team_id=game["homeTeam"]["id"],
            opponent_id=game["awayTeam"]["id"],
        )
        away_team = TeamInfo(
            team_name=game["awayTeam"]["placeName"]["default"],
            team_abbr=game["awayTeam"]["abbrev"],
            season=game["season"],
            team_id=game["awayTeam"]["id"],
            opponent_id=game["homeTeam"]["id"],
        )

        teams.append(home_team)
        teams.append(away_team)

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


def make_predictions(teams, players, date):
    payload = {
        "method": "GET_ALL",
    }
    data = invoke_lambda("Api", payload)
    new_players = json.loads(data.get("entries", []))

    c_predict(teams, players, new_players, date)
    return players


def gather_odds(players):
    retries = 3
    delay = 1

    url = f"https://sportsbook.draftkings.com/sites/CA-ON/api/v5/eventgroups/{DRAFTKINGS_NHL_ID}/categories/{DRAFTKINGS_GOAL_SCORER_CATEGORY}"
    user_agents = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.2 Safari/605.1.15",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; AS; rv:11.0) like Gecko",
    ]

    for attempt in range(retries):
        try:
            headers = {
                "User-Agent": random.choice(user_agents),
                "Accept": "application/json, text/html",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.google.com",
                "Connection": "keep-alive",
            }

            logger.info("Making request to DraftKings")
            response = requests.get(url, headers=headers, timeout=300)
            response.raise_for_status()
            data = response.json()

            if "eventGroup" in data:
                player_infos = []
                for category in data["eventGroup"]["offerCategories"]:
                    if category.get("name") == "Goalscorer":
                        for subcategory in category.get("offerSubcategoryDescriptors", []):
                            if subcategory.get("name") == "Goalscorer":
                                offers = subcategory["offerSubcategory"]["offers"]
                                for offer_group in offers:
                                    for offer in offer_group:
                                        if offer["label"] == "Anytime Goalscorer":
                                            for outcome in offer["outcomes"]:
                                                if outcome.get("providerId") == 2:
                                                    if not (player_name := outcome.get("playerNameIdentifier")):
                                                        player_name = outcome.get("participant")
                                                    odds = outcome["oddsAmerican"]
                                                    player_infos.append(
                                                        {
                                                            "name": player_name,
                                                            "odds": odds,
                                                        }
                                                    )
                link_odds(players, player_infos)
                break

            else:
                logger.error("Currently no Goalscorer bets on DraftKings")
                raise ValueError("Currently no Goalscorer bets on DraftKings")

        except (requests.Timeout, requests.ConnectionError) as e:
            logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay} seconds...")
            time.sleep(delay)
        except requests.RequestException as e:
            logger.error(f"Failed to retrieve data: {e}")
            raise ValueError("Failed to retrieve data")

    else:
        logger.error("Max retries reached. Could not gather odds.")
        raise ValueError("Max retries reached. Could not gather odds.")

    return players


def get_tims(players):
    group_ids = get_tims_players()

    player_table = {player.id: player for player in players}
    for i in range(3):
        for id in group_ids[i]:
            if player_table.get(id):
                player_table[id].set_tims(i + 1)
            else:
                print(f"Player id {id} not found in player list")

    return players


def backfill_dates():
    today = get_date()
    response = invoke_lambda("Api", {"method": "GET_DATES_NO_SCORED"})
    body = response.get("body", {})
    dates_no_scored = json.loads(body.get("dates", "[]"))

    # remove dates that are in the future (shouldn't happen, except maybe today's date)
    dates_no_scored = [date for date in dates_no_scored if date < today]
    logger.info(f"Dates to backfill: {dates_no_scored}")
    if not dates_no_scored:
        return

    scorers_dict = {}
    for date in dates_no_scored:
        data = requests.get(f"https://api-web.nhle.com/v1/score/{date}").json()

        # get players who actually played
        players = []
        for game in data.get('games'):
            players.extend(list({goal.get('playerId') for goal in game.get('goals', {})}))
        scorers_dict[date] = players

    print(scorers_dict)
    response = invoke_lambda("Api", {"method": "POST_BACKFILL", "data": scorers_dict})
    return response


def publish_public_db(teams, players):
    date = get_date()
    players_to_save = []

    team_data = {team.team_id: TEAM_INFO_SCHEMA.dump(team) for team in teams}
    for player in players:
        team_info = team_data[player.team_id]
        team_info_filtered = {
            key: value for key, value in team_info.items()
            if key not in ('team_id', 'opponent_id', 'season', 'team_abbr', 'tims', 'odds', 'stat')
        }

        player_data = PLAYER_INFO_SCHEMA.dump(player)
        player_info_filtered = {
            key: value for key, value in player_data.items()
            if key not in ('id', 'team_id', 'odds')
        }

        players_to_save.append({
            "date": date,
            **player_info_filtered,
            **team_info_filtered
        })

    save_to_db(players_to_save)