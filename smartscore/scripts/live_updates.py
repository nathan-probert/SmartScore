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

# API updates around 15 seconds, using 10 to account for load time etc.
SLEEP_TIME = 10
scores = {}
goal_scorers = {}
# Track the last time a player scored
recent_scorers = {}
SCORER_HIGHLIGHT_TIME = 30  # Time in seconds to keep a scorer highlighted


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

            goal_period = scoring_play.get("periodDescriptor", {}).get("number")
            time_in_goal_period = goal.get("timeInPeriod", "00:00")
            goal_time = (
                ((goal_period - 1) * 1200)
                + (60 * int(time_in_goal_period.split(":")[0]))
                + int(time_in_goal_period.split(":")[1])
            )

            if player_name not in cur_goal_scorers:
                cur_goal_scorers[player_name] = {"teamAbbr": team_abbrev, "time": goal_time, "goal": 0}
            cur_goal_scorers[player_name]["goal"] += 1

    result = defaultdict(list)
    for index, value in cur_goal_scorers.items():
        player_name = index
        player_info = {"name": player_name, "time": value["time"], "goal": value["goal"]}
        result[value["teamAbbr"]].append(player_info)

    return dict(result)


def get_overview():
    URL = "https://api-web.nhle.com/v1/scoreboard/now"
    response = requests.get(URL, timeout=5).json()

    today = get_date()
    output = []

    for day in response.get("gamesByDate", []):
        if day.get("date") == today:
            for game in day.get("games", []):
                if game.get("gameState") in ["LIVE", "CRIT"]:
                    period = game.get("periodDescriptor", {}).get("number", 0)
                    time_remaining = game.get("clock", {}).get("timeRemaining", "00:00")
                    is_intermission = bool(game.get("clock", {}).get("inIntermission", False))
                    total_time_passed = (period - 1) * 1200 + (
                        20 * 60 - int(time_remaining.split(":")[0]) * 60 - int(time_remaining.split(":")[1])
                    )

                    away_team = game.get("awayTeam", {}).get("name", {}).get("default", "Unknown Team")
                    away_abbr = game.get("awayTeam", {}).get("abbrev", "")

                    home_team = game.get("homeTeam", {}).get("name", {}).get("default", "Unknown Team")
                    home_abbr = game.get("homeTeam", {}).get("abbrev", "")

                    away_score = game.get("awayTeam", {}).get("score", 0)
                    home_score = game.get("homeTeam", {}).get("score", 0)
                    game_id = game.get("id")

                    if game_id is not None:
                        # Check and update scores
                        if not scores.get(game_id) or scores[game_id] != (away_score, home_score):
                            scores[game_id] = (away_score, home_score)
                            goal_scorers[game_id] = get_goal_scorers(game_id)

                        # Initialize flag for green players
                        has_green_scorer = False

                        # Append game information to output
                        away_scorers = []
                        for goal in goal_scorers[game_id].get(away_abbr, []):
                            if (total_time_passed - goal["time"] < 60) and not is_intermission:
                                away_scorers.append(f"{GREEN}{goal['name']} ({goal['goal']}){RESET}")
                                has_green_scorer = True  # Mark that we have a green scorer
                            else:
                                away_scorers.append(f"{goal['name']} ({goal['goal']})")

                        home_scorers = []
                        for goal in goal_scorers[game_id].get(home_abbr, []):
                            same_period = period == goal["time"] // 1200 + 1
                            if (total_time_passed - goal["time"] < 60) and same_period and not is_intermission:
                                home_scorers.append(f"{GREEN}{goal['name']} ({goal['goal']}){RESET}")
                                has_green_scorer = True  # Mark that we have a green scorer
                            else:
                                home_scorers.append(f"{goal['name']} ({goal['goal']})")

                        # Set the color for the game header based on the presence of green players
                        header_color = BLUE if has_green_scorer else ""
                        output.append("")
                        if is_intermission:
                            output.append(
                                f"{header_color}Intermission {period} | {time_remaining} | {home_team} @ {away_team}{RESET}"
                            )
                        else:
                            output.append(
                                f"{header_color}Period {period} | {time_remaining} | {home_team} @ {away_team}{RESET}"
                            )
                        output.append(f"{home_score} - {away_score}")

                        # Append formatted scorer output
                        output.append(f"  {away_abbr}: {', '.join(away_scorers)}")
                        output.append(f"  {home_abbr}: {', '.join(home_scorers) if home_scorers else ''}")

    # Join all lines into a single string for output
    return "\n".join(output)


if __name__ == "__main__":
    print("Starting now...")
    while True:
        game_overview = get_overview()
        clear_terminal()
        print(game_overview if game_overview else "No live games at the moment.")
        time.sleep(SLEEP_TIME)
