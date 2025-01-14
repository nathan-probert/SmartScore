import os
import sys
import time
from collections import defaultdict

import requests

sys.path.append("D:\\code\\smartScore\\smartscore")
from service import get_date  # noqa: E402

# ANSI escape code for green text
GREEN = "\033[92m"
BLUE = "\033[34m"
RESET = "\033[0m"

# API updates around 15 seconds
SLEEP_TIME = 15
RECENT_GOAL_TIME = 60

scores = {}
goal_scorers = {}
recent_scorers = {}
SCORER_HIGHLIGHT_TIME = 30  # Time in seconds to keep a scorer highlighted

# create from get_odds get_names function, can't be generated dynamically as games will have started
WATCHLIST = {
    "Toronto": [
        "Auston Matthews"
    ],
}


def clear_terminal():
    # Clear the terminal screen in a cross-platform way
    os.system("cls" if os.name == "nt" else "clear")


def get_goal_scorers(game_id):
    URL = f"https://api-web.nhle.com/v1/wsc/game-story/{game_id}"
    response = requests.get(URL, timeout=5).json()

    cur_goal_scorers = {}
    for scoring_play in response.get("summary", {}).get("scoring", []):
        for goal in scoring_play.get("goals", []):
            player_name = goal.get("name", {}).get("default", "Unknown Player")
            team_abbrev = goal.get("teamAbbrev", {}).get("default", "")
            full_name = goal.get("firstName", {}).get("default", "") + " " + goal.get("lastName", {}).get("default", "")

            goal_period = scoring_play.get("periodDescriptor", {}).get("number")
            time_in_goal_period = goal.get("timeInPeriod", "00:00")
            goal_time = (
                ((goal_period - 1) * 1200)
                + (60 * int(time_in_goal_period.split(":")[0]))
                + int(time_in_goal_period.split(":")[1])
            )

            if player_name not in cur_goal_scorers:
                cur_goal_scorers[player_name] = {
                    "teamAbbr": team_abbrev,
                    "time": goal_time,
                    "goal": 0,
                    "full_name": full_name,
                }
            cur_goal_scorers[player_name]["goal"] += 1

    result = defaultdict(list)
    for index, value in cur_goal_scorers.items():
        player_name = index
        player_info = {
            "name": player_name,
            "time": value["time"],
            "goal": value["goal"],
            "full_name": value["full_name"],
        }
        result[value["teamAbbr"]].append(player_info)

    return dict(result)


def get_scorers_str(game, scorers, watchlist):
    scorers_str = []
    for goal in scorers:
        if goal["full_name"] in watchlist:
            scorers_str.append(f"{GREEN}{goal['name']} ({goal['goal']}){RESET}")
        elif (
            ((game.get("time_passed") - goal["time"]) < RECENT_GOAL_TIME)
            and (game.get("period") == goal["time"] // 1200 + 1)
            and not game.get("is_intermission")
            and game.get("status") in ["LIVE", "CRIT"]
        ):
            scorers_str.append(f"{BLUE}{goal['name']} ({goal['goal']}){RESET}")

            game.get("has_recent_scorer", True)
        else:
            scorers_str.append(f"{goal['name']} ({goal['goal']})")

    return scorers_str


def get_scoreboard():
    games = {}

    URL = "https://api-web.nhle.com/v1/scoreboard/now"
    response = requests.get(URL, timeout=5).json()
    today = get_date()

    for day in response.get("gamesByDate", []):
        if day.get("date") == today:
            for game in day.get("games", []):
                away_team = game.get("awayTeam", {}).get("placeNameWithPreposition", {}).get("default", "Unknown Team")
                away_abbr = game.get("awayTeam", {}).get("abbrev", "")
                away_score = game.get("awayTeam", {}).get("score", 0)

                home_team = game.get("homeTeam", {}).get("placeNameWithPreposition", {}).get("default", "Unknown Team")
                home_abbr = game.get("homeTeam", {}).get("abbrev", "")
                home_score = game.get("homeTeam", {}).get("score", 0)

                status = game.get("gameState")
                period = game.get("periodDescriptor", {}).get("number", 0)
                period_time_remaining = game.get("clock", {}).get("timeRemaining", "00:00")
                is_intermission = bool(game.get("clock", {}).get("inIntermission", False))
                time_passed = ((period - 1) * 1200) + (20 * 60 - int(game.get("clock", {}).get("secondsRemaining", 0)))

                id = game.get("id")

                games[id] = {
                    "away_team": away_team,
                    "away_abbr": away_abbr,
                    "away_score": away_score,
                    "home_team": home_team,
                    "home_abbr": home_abbr,
                    "home_score": home_score,
                    "status": status,
                    "period": period,
                    "period_time_remaining": period_time_remaining,
                    "is_intermission": is_intermission,
                    "time_passed": time_passed,
                }

    return games


def get_overview():
    output = []

    scoreboard = get_scoreboard()

    for gameId, game in scoreboard.items():
        if game["status"] in ["LIVE", "CRIT", "FINAL"]:
            if not scores.get(gameId) or scores[gameId] != (game["away_score"], game["home_score"]):
                scores[gameId] = (game["away_score"], game["home_score"])
                goal_scorers[gameId] = get_goal_scorers(gameId)

            game["has_recent_scorer"] = False
            home_scorers = get_scorers_str(
                game, goal_scorers[gameId].get(game["home_abbr"], []), WATCHLIST.get(game["home_team"], [])
            )
            away_scorers = get_scorers_str(
                game, goal_scorers[gameId].get(game["away_abbr"], []), WATCHLIST.get(game["away_team"], [])
            )

            header_color = BLUE if game["has_recent_scorer"] else ""
            state = "Intermission" if game["is_intermission"] else "Period"

            output.append("")
            if game.get("gameState") == "FINAL":
                output.append(
                    f"{header_color}{game.get('gameState')} | {game.get('home_team')} @ {game.get('away_team')}{RESET}"
                )
            else:
                output.append(
                    f"{header_color}{state} {game.get('period')} | {game.get('period_time_remaining')} | {game.get('home_team')} @ {(game.get('away_team'))}{RESET}"
                )
            output.append(f"{game.get('home_score')} - {game.get('away_score')}")

            output.append(f"  {game.get('home_abbr')}: {', '.join(home_scorers) if home_scorers else ''}")
            output.append(f"  {game.get('away_abbr')}: {', '.join(away_scorers)}")

    # Join all lines into a single string for output
    return "\n".join(output)


if __name__ == "__main__":
    print("Starting now...")
    while True:
        game_overview = get_overview()
        clear_terminal()
        print(game_overview if game_overview else "No live games at the moment.")
        time.sleep(SLEEP_TIME)
