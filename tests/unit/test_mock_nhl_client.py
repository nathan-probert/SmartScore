import json

import pytest
from smartscore_info_client.models.player import PlayerStats
from smartscore_info_client.models.team import TeamStats

from mock_nhl_client import MockNHLClient


@pytest.fixture
def fixtures_dir(tmp_path):
    base = tmp_path

    # Schedule for a single game on the frozen day.
    schedule = {
        "gameWeek": [
            {
                "games": [
                    {
                        "startTimeUTC": "2025-06-11T23:00:00Z",
                        "season": "20242025",
                        "homeTeam": {"id": 1, "abbrev": "TOR"},
                        "awayTeam": {"id": 2, "abbrev": "EDM"},
                    }
                ]
            }
        ]
    }

    roster = {
        "forwards": [
            {
                "id": 100,
                "firstName": {"default": "John"},
                "lastName": {"default": "Doe"},
            }
        ],
        "defensemen": [],
        "goalies": [],
    }

    player_landing = {
        "seasonTotals": [
            {
                "season": 20242025,
                "leagueAbbrev": "NHL",
                "goals": 40,
                "gamesPlayed": 80,
                "powerPlayGoals": 10,
            }
        ],
        "last5Games": [{"goals": 1}, {"goals": 0}, {"goals": 1}, {"goals": 0}, {"goals": 1}],
    }

    team_summary = {
        "data": [
            {"teamId": 1, "goalsForPerGame": 3.4, "goalsAgainstPerGame": 2.8},
            {"teamId": 2, "goalsForPerGame": 3.0, "goalsAgainstPerGame": 2.5},
        ]
    }

    penalty_kill = {
        "data": [
            {"teamId": 1, "shorthandedGoalsAgainst": 3, "gamesPlayed": 80},
            {"teamId": 2, "shorthandedGoalsAgainst": 4, "gamesPlayed": 80},
        ]
    }

    score = {"games": []}

    files = {
        "schedule/2025-06-11.json": schedule,
        "score/2025-06-11.json": score,
        "roster/TOR.json": roster,
        "roster/EDM.json": roster,
        "player/100.json": player_landing,
        "team/summary_20242025.json": team_summary,
        "team/penalty_kill_20242025.json": penalty_kill,
    }
    for relative, payload in files.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    return base


def test_schedule(fixtures_dir):
    client = MockNHLClient(fixtures_dir)
    data = client.get_schedule("2025-06-11")
    assert data["gameWeek"][0]["games"][0]["season"] == "20242025"


def test_player_stats_parsed(fixtures_dir):
    client = MockNHLClient(fixtures_dir)
    stats = client.get_player_stats(100)
    assert isinstance(stats, PlayerStats)
    # 40 goals / 80 games = 0.5 gpg
    assert stats.gpg == 0.5
    # 3 goals / 5 games last 5
    assert stats.five_gpg == 0.6
    # 10 / 80 power play goals per game
    assert stats.hppg == 0.125


def test_team_stats_parsed(fixtures_dir):
    client = MockNHLClient(fixtures_dir)
    stats = client.get_team_stats("20242025", 1, 2)
    assert isinstance(stats, TeamStats)
    assert stats.tgpg == 3.4
    assert stats.otga == 2.5
    # opponent (team 2) shorthanded goals against per game
    assert stats.otshga == 4 / 80


def test_missing_fixture_raises(fixtures_dir, tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    client = MockNHLClient(empty_dir)
    with pytest.raises(FileNotFoundError):
        client.get_schedule("2025-06-11")
