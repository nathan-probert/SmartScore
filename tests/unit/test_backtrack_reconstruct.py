"""reconstruct_player: strictly-before gpg, the debut-zero convention, context."""

import sys

import reconstruct
from reconstruct import _rate, main, reconstruct_player


def _game(date, goals=0, team="BOS", home=True, **extra):
    game = {
        "gameDate": date,
        "goals": goals,
        "teamAbbrev": team,
        "homeRoadFlag": "H" if home else "A",
    }
    game.update(extra)
    return game


def test_first_game_row_is_emitted_with_zero_gpg_not_null():
    # The archive stores 0 on pre-debut rows even when the player scored that
    # very night, so a null here would fail the drop-in comparison.
    rows, game_count = reconstruct_player(8476967, "20232024", [_game("2024-02-19", goals=1)])

    assert game_count == 1
    assert len(rows) == 1
    assert rows[0]["gpg"] is not None
    assert rows[0]["gpg"] == 0.0
    assert rows[0]["date"] == "2024-02-19"
    assert rows[0]["player_id"] == 8476967


def test_gpg_counts_games_strictly_before_the_row_date():
    log = [
        _game("2023-10-14", goals=2),
        _game("2023-10-10", goals=1),
        _game("2023-10-12", goals=0),
    ]
    rows, _ = reconstruct_player(201, "20232024", log)

    by_date = {r["date"]: r["gpg"] for r in rows}
    assert by_date["2023-10-10"] == 0.0  # debut: 0 goals in 0 games
    assert by_date["2023-10-12"] == 1.0  # 1 goal in 1 prior game
    assert by_date["2023-10-14"] == 0.5  # 1 goal in 2 prior games


def test_one_row_per_game_and_name_fallback_matches_publish():
    log = [_game("2023-10-10"), _game("2023-10-12")]

    rows, game_count = reconstruct_player(12345, "20232024", log)
    assert game_count == len(log)
    assert len(rows) == 2
    assert all(r["name"] == "player-12345" for r in rows)

    rows, _ = reconstruct_player(12345, "20232024", log, name="Connor McDavid")
    assert all(r["name"] == "Connor McDavid" for r in rows)


def test_boxscore_context_overrides_the_game_log_team():
    # The game log drops teamAbbrev on some rows; the box score never does.
    log = [_game("2023-10-10", team="BOS", home=True)]
    appearances = {("2023-10-10", 77): {"team_abbrev": "MTL", "home": False}}

    rows, _ = reconstruct_player(77, "20232024", log, appearances=appearances)
    assert rows[0]["team_name"] == "Montréal"
    assert rows[0]["home"] is False

    rows, _ = reconstruct_player(77, "20232024", log)
    assert rows[0]["team_name"] == "Boston"
    assert rows[0]["home"] is True


def test_rate_is_none_only_without_games():
    assert _rate(4, 2) == 2.0
    assert _rate(0, 3) == 0.0
    assert _rate(1, 1) == 1.0
    assert _rate(5, 0) is None
    assert _rate(0, 0) is None


class _ArchiveQuery:
    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def gte(self, *args, **kwargs):
        return self

    def lte(self, *args, **kwargs):
        return self

    def execute(self):
        class _Result:
            data = []

        return _Result()


class _FakeSupabase:
    def table(self, name):
        return _ArchiveQuery()


def test_main_player_branch_runs_without_boxscore_discovery(monkeypatch):
    # Regression: the --player branch once left appearances unbound, crashing
    # with UnboundLocalError after the game log had already been fetched.
    game = _game("2023-10-10", goals=1, team="EDM")
    monkeypatch.setattr(reconstruct, "fetch_player_name", lambda pid, delay_seconds=0: "A Player")
    monkeypatch.setattr(reconstruct, "fetch_game_log", lambda pid, season, delay_seconds=0: [game])
    monkeypatch.setattr(reconstruct, "config", lambda: ("dev", _FakeSupabase()))
    monkeypatch.setattr(sys, "argv", ["reconstruct.py", "--player", "8476967", "--dry-run"])

    assert main() == 0
