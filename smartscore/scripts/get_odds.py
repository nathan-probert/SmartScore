"""
Usage

ENV=prod poetry run python smartscore/scripts/get_odds.py
Use prod to access the product database as it will be kept up to date
"""

import secrets
import sys
import time

import requests
from aws_lambda_powertools import Logger

sys.path.append("D:\\code\\smartScore\\smartscore")
from constants import DRAFTKINGS_GOAL_SCORER_CATEGORY, DRAFTKINGS_NHL_ID, DRAFTKINGS_PROVIDER_ID  # noqa: E402
from utility import get_today_db  # noqa: E402

logger = Logger()


def adjust_name(df_name):
    name_replacements = {
        "Cam": "Cameron",
        "J.J. Moser": "Janis Moser",
        "Pat Maroon": "Patrick Maroon",
        "T.J. Brodie": "TJ Brodie",
        "Mitchell Marner": "Mitch Marner",
        "Alex Wennberg": "Alexander Wennberg",
    }
    for old_name, new_name in name_replacements.items():
        df_name = df_name.replace(old_name, new_name)

    return df_name


def link_odds(players, player_infos):
    player_table = {player["name"]: player for player in players}
    for player_info in player_infos:
        if player_table.get(player_info["name"]):
            player_table[player_info["name"]]["odds"] = player_info["odds"]
        else:
            # retry with adjustments
            player_info["name"] = adjust_name(player_info["name"])
            if player_table.get(player_info["name"]):
                player_table[player_info["name"]]["odds"] = player_info["odds"]
            else:
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
            response = requests.get(url, headers=headers, timeout=300)
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
    return min(bet_amount, bankroll)


if __name__ == "__main__":
    players = get_today_db()
    players = gather_odds(players)
    players = convert_to_percent(players)

    bankroll_per_person = 100  # bet up to this amount per person
    print("Name, my probability, DraftKings probability, DraftKings odds")
    for player in players:
        stat = player.get("stat") * 100
        odds = player.get("percent_odds")

        bet_size = calculate_bet_size(player, bankroll_per_person)
        if bet_size > 0:
            print(f"{player['name']}, {player['stat']*100:.2f}%, {player['percent_odds']:.2f}%, {player['odds']}")
            print(f"Bet size: ${bet_size:.2f}")
            print()
