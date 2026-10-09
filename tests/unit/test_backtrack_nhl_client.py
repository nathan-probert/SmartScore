"""season_games: playoffs are walked and included, preseason is not."""

import nhl_client


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
