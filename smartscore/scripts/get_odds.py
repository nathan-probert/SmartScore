"""
Usage

ENV=prod poetry run python smartscore/scripts/get_odds.py
Use prod to access the product database as it will be kept up to date

DOES NOT CURRENTLY TAKE INTO ACCOUNT THAT SOME GAMES MAY HAVE STARTED
e.g if game starts, df odds will be higher (+100 -> +200 etc) because time has already passed in the game
My probability does not take that into account, so the program will believe this to be a very good bet
"""

import csv
import json
import os
import secrets
import sys
import time
from collections import defaultdict
from datetime import datetime

import requests
from aws_lambda_powertools import Logger
from unidecode import unidecode

sys.path.append("../smartscore")

from constants import DRAFTKINGS_GOAL_SCORER_CATEGORY, DRAFTKINGS_NHL_ID, DRAFTKINGS_PROVIDER_ID  # noqa: E402
from utility import adjust_name, get_today_db  # noqa: E402

logger = Logger()

MINIMUM_BET_SIZE = 0.1


def ignore_player(df_name):
    ignore = {"Brendan Brisson"}
    return df_name in ignore


def link_odds(players, player_infos):
    player_table = {unidecode(player["name"]): player for player in players}
    for player_info in player_infos:
        if player_table.get(unidecode(player_info["name"])):
            player_table[player_info["name"]]["odds"] = player_info["odds"]
        else:
            # retry with adjustments
            player_info["name"] = adjust_name(player_info["name"])
            if player_table.get(player_info["name"]):
                player_table[player_info["name"]]["odds"] = player_info["odds"]
            elif not ignore_player(player_info["name"]):
                for player in players:
                    print(player["name"])
                print(f"Player {player_info['name']} not found in player list")
                print("Add this discrepancy to the adjust_name function")
                sys.exit(1)


def fetch_draftkings_data(url, user_agents, retries=3, delay=1):
    """Fetch data from DraftKings API with retry logic."""
    for attempt in range(retries):
        try:
            headers = {
                "User-Agent": secrets.choice(user_agents),
                "Accept": "application/json, text/html",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.google.com",
                "Connection": "keep-alive",
            }
            logger.info("Making request to DraftKings")
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            return response.json()
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay} seconds...")
            time.sleep(delay)
        except requests.RequestException as e:
            logger.error(f"Failed to retrieve data: {e}")
            break

    logger.error("Max retries reached. Could not gather odds.")
    return None


def extract_player_odds(data):
    """Extract player odds from DraftKings response data."""
    player_infos = []
    event_group = data.get("eventGroup", {})
    for category in event_group.get("offerCategories", []):
        if category.get("name") == "Goalscorer":
            for subcategory in category.get("offerSubcategoryDescriptors", []):
                if subcategory.get("name") == "Goalscorer":
                    offers = subcategory.get("offerSubcategory", {}).get("offers", [])
                    for offer_group in offers:
                        for offer in offer_group:
                            if offer["label"] == "Anytime Goalscorer":
                                for outcome in offer["outcomes"]:
                                    if outcome.get("providerId") == DRAFTKINGS_PROVIDER_ID:
                                        player_name = outcome.get("playerNameIdentifier") or outcome.get("participant")
                                        odds = outcome["oddsAmerican"]
                                        player_infos.append({"name": player_name, "odds": odds})
    return player_infos


def gather_odds(players):
    url = f"https://sportsbook.draftkings.com/sites/CA-ON/api/v5/eventgroups/{DRAFTKINGS_NHL_ID}/categories/{DRAFTKINGS_GOAL_SCORER_CATEGORY}"
    user_agents = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.2 Safari/605.1.15",  # noqa: E501
        "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",  # noqa: E501
        "Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; AS; rv:11.0) like Gecko",
    ]

    data = fetch_draftkings_data(url, user_agents)
    if not data:
        raise ValueError("Max retries reached. Could not gather odds.")

    player_infos = extract_player_odds(data)
    if player_infos:
        link_odds(players, player_infos)
    else:
        logger.error("Currently no Goalscorer bets on DraftKings")
        raise ValueError("Currently no Goalscorer bets on DraftKings")

    return players


def convert_to_percent(players):
    for player in players:
        odds = int(player.get("odds", 0))

        if odds < 0:
            odds = abs(odds)
            player["percent_odds"] = odds / (odds + 100) * 100
        elif odds > 0:
            player["percent_odds"] = 100 / (odds + 100) * 100

    return players


def calculate_bet_size(player, bankroll):
    if not player.get("stat") or not player.get("percent_odds"):
        return 0

    p = player["stat"]
    q = player["percent_odds"] / 100

    payout_odds = int(player["odds"])
    if payout_odds >= 0:
        decimal_odds = (payout_odds / 100) + 1
    else:
        decimal_odds = (100 / abs(payout_odds)) + 1

    edge = p - q
    kelly_fraction = edge / (decimal_odds - 1)

    bet_amount = max(0, kelly_fraction * bankroll)
    return round(min(bet_amount, bankroll), 2)


def get_names():
    result = defaultdict(list)

    players = get_today_db()
    players = gather_odds(players)
    players = convert_to_percent(players)

    bankroll_per_person = 100
    for player in players:
        bet_size = calculate_bet_size(player, bankroll_per_person)
        if bet_size > MINIMUM_BET_SIZE:
            _payout_amount = bet_size + (bet_size * float(player["odds"]) / 100)

            result[player["team_name"]].append(player["name"])

    print(result)
    return result


if __name__ == "__main__":
    players = get_today_db()
    players = gather_odds(players)
    players = convert_to_percent(players)

    bankroll_per_person = 100  # bet up to this amount per person
    print("\n\nName, Team, my probability, DraftKings probability, DraftKings odds\n")

    total_bets = 0
    total_possible_payout = 0

    # create file if it does not exist

    try:
        with open("./lib/bets.csv", mode="r") as file:
            reader = csv.reader(file)
            data = list(reader)
            last_date = data[-1][0]
    except FileNotFoundError:
        os.makedirs("./lib", exist_ok=True)
        with open("./lib/bets.csv", "w") as file:
            writer = csv.writer(file)
            writer.writerow(
                [
                    "Date",
                    "Name",
                    "Team",
                    "My Probability",
                    "DraftKings Probability",
                    "DraftKings Odds",
                    "Bet Size",
                    "Payout Amount",
                    "Scored",
                ]
            )

    with open("./lib/bets.csv", mode="a", newline="") as file:
        writer = csv.writer(file)

        for player in players:
            stat = player.get("stat") * 100
            odds = player.get("percent_odds")

            bet_size = calculate_bet_size(player, bankroll_per_person)
            if bet_size > MINIMUM_BET_SIZE:
                payout_amount = bet_size + (bet_size * float(player["odds"]) / 100)
                date_today = datetime.now().strftime("%Y-%m-%d")

                total_bets += bet_size
                total_possible_payout += payout_amount

                print(
                    f"{player['name']}, "
                    f"{player['team_name']}, "
                    f"{player['stat'] * 100:.2f}%, "
                    f"{player['percent_odds']:.2f}%, "
                    f"{player['odds']}"
                )
                print(f"Bet size: ${bet_size:.2f}, Return: ${payout_amount:.2f}")
                print()

                if date_today != last_date:
                    writer.writerow(
                        [
                            date_today,
                            player["name"],
                            player["team_name"],
                            f"{stat:.2f}",
                            f"{odds:.2f}",
                            player["odds"],
                            f"{bet_size:.2f}",
                            f"{payout_amount:.2f}",
                            player.get("scored", ""),  # Default to an empty string if "scored" is not available
                        ]
                    )

    print(f"Total bets: ${total_bets:.2f}")
    print(f"Total possible payout: ${total_possible_payout:.2f}")

    print(f"Info for live updates: \n{json.dumps(get_names(), indent=4)}")
