"""
Record frozen NHL API fixtures for a chosen real game day.

Writes raw API payloads into ``smartscore/fixtures/nhl/`` in the exact layout
that :class:`smartscore.mock_nhl_client.MockNHLClient` expects, so deployed
Lambdas can run in mock mode (off-season / integration tests) with realistic
data.

Usage:
    poetry run python -m smartscore.scripts.record_nhl_fixtures 2025-06-11

The date should be a real day on which NHL games were played so the recorded
schedule/rosters/player-stats/team-stats correspond to actual games.
"""

import argparse
import json
import sys
from pathlib import Path

from smartscore_info_client.api.nhle import NHLClient

CLIENT = NHLClient()

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "nhl"
SCHEDULE_FILE = "schedule/{date}.json"
SCORE_FILE = "score/{date}.json"
ROSTER_FILE = "roster/{team}.json"
PLAYER_FILE = "player/{player_id}.json"
TEAM_SUMMARY_FILE = "team/summary_{season}.json"
TEAM_PENALTY_KILL_FILE = "team/penalty_kill_{season}.json"


def _write(relative_path: str, payload) -> None:
    target = FIXTURES_DIR / relative_path
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"  wrote {target}")


def record(date: str) -> None:
    print(f"Fetching schedule for {date}")
    schedule = CLIENT.get_schedule(date)
    _write(SCHEDULE_FILE.format(date=date), schedule)

    print("Fetching score")
    score = CLIENT.get_score(date)
    _write(SCORE_FILE.format(date=date), score)

    seasons = set()
    teams = set()
    for week in schedule.get("gameWeek", []):
        for game in week.get("games", []):
            season = game.get("season")
            if season:
                seasons.add(season)
            for side in ("homeTeam", "awayTeam"):
                teams.add(game[side].get("abbrev"))

    rosters = {}
    for team in sorted(teams):
        print(f"Fetching roster for {team}")
        roster = CLIENT.get_roster(team)
        _write(ROSTER_FILE.format(team=team), roster)
        rosters[team] = roster

    for season in seasons:
        print(f"Fetching team summary for {season}")
        summary = CLIENT.get_team_summary(season)
        _write(TEAM_SUMMARY_FILE.format(season=season), summary)

        print(f"Fetching team penalty kill for {season}")
        penalty_kill = CLIENT.get_team_penalty_kill(season)
        _write(TEAM_PENALTY_KILL_FILE.format(season=season), penalty_kill)

    player_ids = set()
    for roster in rosters.values():
        for player_type in ("forwards", "defensemen", "goalies"):
            for player in roster.get(player_type, []):
                player_id = player.get("id")
                if player_id:
                    player_ids.add(player_id)

    for player_id in sorted(player_ids):
        print(f"Fetching player landing for {player_id}")
        landing = CLIENT.get_player_landing(player_id)
        _write(PLAYER_FILE.format(player_id=player_id), landing)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", help="Real game day to record, e.g. 2025-06-11")
    args = parser.parse_args()
    record(args.date)
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
