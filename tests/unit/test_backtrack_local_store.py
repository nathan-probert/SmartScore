"""local_store window SQL: strictly-before gpg/five_gpg and team otga semantics."""

import local_store
import pytest


def _insert(conn, player_id, game_id, date, goals, team=None, tgf=None, season="20232024"):
    conn.execute(
        "INSERT INTO player_games (player_id, game_id, season, game_date, goals,"
        " team_abbrev, team_goals_for) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (player_id, game_id, season, date, goals, team, tgf),
    )


def test_compute_derived_is_strictly_before_and_debut_is_zero(tmp_path):
    conn = local_store.connect(tmp_path / "raw.sqlite")
    _insert(conn, 201, 8001, "2023-10-10", 1)
    _insert(conn, 201, 8002, "2023-10-12", 0)
    _insert(conn, 201, 8003, "2023-10-14", 2)

    local_store.compute_derived(conn, "20232024")
    rows = {r["game_date"]: r for r in conn.execute("SELECT * FROM derived_features")}

    assert len(rows) == 3
    # Debut: 0, not NULL - the archive's pre-debut convention.
    assert rows["2023-10-10"]["gpg"] == 0
    assert rows["2023-10-10"]["five_gpg"] == 0
    # Each later row counts games strictly before its own date.
    assert rows["2023-10-12"]["gpg"] == 1.0
    assert rows["2023-10-14"]["gpg"] == 0.5
    assert rows["2023-10-14"]["five_gpg"] == 0.5


def test_compute_derived_replaces_previous_output(tmp_path):
    conn = local_store.connect(tmp_path / "raw.sqlite")
    _insert(conn, 201, 8001, "2023-10-10", 1)

    local_store.compute_derived(conn, "20232024")
    local_store.compute_derived(conn, "20232024")

    count = conn.execute(
        "SELECT COUNT(*) FROM derived_features WHERE player_id = 201",
    ).fetchone()[0]
    assert count == 1


def test_compute_derived_team_otga_is_the_opponents_goals_against(tmp_path):
    conn = local_store.connect(tmp_path / "raw.sqlite")
    # Game 1: BOS 3, TOR 5. Game 2: BOS 4, TOR 2.
    _insert(conn, 101, 9001, "2023-10-10", 1, team="BOS", tgf=3)
    _insert(conn, 102, 9001, "2023-10-10", 2, team="TOR", tgf=5)
    _insert(conn, 101, 9002, "2023-10-12", 0, team="BOS", tgf=4)
    _insert(conn, 102, 9002, "2023-10-12", 1, team="TOR", tgf=2)

    local_store.compute_derived_team(conn, "20232024")
    rows = {(r["team_abbrev"], r["game_date"]): r for r in conn.execute("SELECT * FROM derived_team_stats")}

    # First game of the season: no rate entering it, so NULL on both sides.
    assert rows[("BOS", "2023-10-10")]["tgpg"] is None
    assert rows[("BOS", "2023-10-10")]["otga"] is None
    assert rows[("TOR", "2023-10-10")]["tgpg"] is None
    assert rows[("TOR", "2023-10-10")]["otga"] is None

    # Game 2, BOS: own goals for entering = 3/1. TOR's goals against entering =
    # the 3 BOS scored in game 1 over TOR's 1 prior game. Same number here only
    # because the fixture is small - they measure different sides.
    assert rows[("BOS", "2023-10-12")]["tgpg"] == 3.0
    assert rows[("BOS", "2023-10-12")]["otga"] == 3.0

    assert rows[("TOR", "2023-10-12")]["tgpg"] == 5.0
    assert rows[("TOR", "2023-10-12")]["otga"] == 5.0

    assert rows[("BOS", "2023-10-12")]["otshga"] is None


def test_compute_derived_team_rejects_games_without_an_opponent(tmp_path):
    conn = local_store.connect(tmp_path / "raw.sqlite")
    _insert(conn, 101, 9001, "2023-10-10", 1, team="BOS", tgf=3)

    # Dropping the orphan silently would make otga wrong rather than absent.
    with pytest.raises(ValueError, match="no opponent"):
        local_store.compute_derived_team(conn, "20232024")
