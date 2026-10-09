"""season_games: playoffs are walked and included, preseason is not.

Also covers the box-score retry envelope: a full-season crawl dies on the first
reset connection without it.
"""

import nhl_client
import requests


class _FakeSchedule:
    """Serves canned schedule windows by date and records what was asked for."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.asked = []

    def __call__(self, date, delay_seconds=0.0):
        self.asked.append(date)
        return self.payloads[date]


def _payload(games, next_start, start="2023-10-10", reg_end="2024-04-18", playoff_end="2024-06-24"):
    payload = {
        "regularSeasonStartDate": start,
        "regularSeasonEndDate": reg_end,
        "gameWeek": [{"date": games[0]["date"] if games else start, "games": games}],
        "nextStartDate": next_start,
    }
    if playoff_end:
        payload["playoffEndDate"] = playoff_end
    return payload


def _game(game_id, game_type, date, season="20232024"):
    return {
        "id": game_id,
        "date": date,
        "gameType": game_type,
        "season": season,
        "gameState": "FINAL",
        "awayTeam": {"abbrev": "TOR"},
        "homeTeam": {"abbrev": "BOS"},
    }


def test_playoff_games_are_walked_in_and_preseason_stays_out(tmp_path, monkeypatch):
    reg = _game(2023020001, 2, "2023-10-10")
    playoff = _game(2023030001, 3, "2024-05-15")
    preseason = _game(2023010001, 1, "2024-05-15")
    fake = _FakeSchedule(
        {
            # The seed only supplies season bounds; its week is never scanned.
            "2024-01-15": _payload([], "2024-06-25"),
            "2023-10-10": _payload([reg, preseason], "2024-04-18"),
            "2024-04-18": _payload([playoff, preseason], "2024-06-25"),
        }
    )
    monkeypatch.setattr(nhl_client, "fetch_schedule", fake)

    games = nhl_client.season_games("20232024", cache_dir=tmp_path)

    assert [g["id"] for g in games] == [2023020001, 2023030001]
    # The walk ran through playoffEndDate, not regularSeasonEndDate.
    assert "2024-04-18" in fake.asked
    # Cached: the file now holds the full-season list.
    assert (tmp_path / "schedule-20232024.json").exists()


def test_without_a_playoff_bound_the_walk_stops_at_regular_season_end(tmp_path, monkeypatch):
    reg = _game(2023020001, 2, "2023-10-10")
    late_playoff = _game(2023030001, 3, "2024-04-25")
    fake = _FakeSchedule(
        {
            "2024-01-15": _payload([], "2024-06-25", playoff_end=None),
            "2023-10-10": _payload([reg], "2024-04-18", playoff_end=None),
            "2024-04-18": _payload([], "2024-04-25", playoff_end=None),
            "2024-04-25": _payload([late_playoff], "2024-05-02", playoff_end=None),
        }
    )
    monkeypatch.setattr(nhl_client, "fetch_schedule", fake)

    games = nhl_client.season_games("20232024", cache_dir=tmp_path)

    assert [g["id"] for g in games] == [2023020001]
    # The window past regularSeasonEndDate was never requested.
    assert "2024-04-25" not in fake.asked


class _FakeBoxscoreResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_fetch_boxscore_retries_reset_connections(tmp_path, monkeypatch):
    payload = {
        "gameDate": "2024-05-15",
        "homeTeam": {"abbrev": "BOS", "score": 3},
        "awayTeam": {"abbrev": "TOR", "score": 2},
        "playerByGameStats": {
            "homeTeam": {
                "forwards": [
                    {
                        "playerId": 301,
                        "name": {"default": "P. Layer"},
                        "position": "C",
                        "sweaterNumber": 9,
                        "goals": 1,
                        "assists": 2,
                        "points": 3,
                        "shots": 4,
                        "pim": 0,
                        "toi": "12:34",
                        "plusMinus": 1,
                        "shifts": 20,
                        "powerPlayGoals": 1,
                    }
                ],
                "defense": [],
                "goalies": [],
            },
            "awayTeam": {"forwards": [], "defense": [], "goalies": []},
        },
    }
    calls = {"n": 0}

    def flaky_get(url, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("connection reset by peer")
        return _FakeBoxscoreResponse(payload)

    monkeypatch.setattr(nhl_client.requests, "get", flaky_get)
    monkeypatch.setattr(nhl_client.time, "sleep", lambda seconds: None)

    records = nhl_client.fetch_boxscore(2023030001, delay_seconds=0.0, cache_dir=tmp_path)

    assert calls["n"] == 3
    assert [r["player_id"] for r in records] == [301]
    assert records[0]["game_id"] == 2023030001
    assert records[0]["goals"] == 1
