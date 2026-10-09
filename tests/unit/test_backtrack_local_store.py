"""local_store window SQL: strictly-before gpg/five_gpg/ppg, team otga/otshga, publish names."""

from types import SimpleNamespace

import local_store
import pytest


def _insert(  # noqa: PLR0913, PLR0917 - helper mirrors the table's shape; every extra has a default
    conn,
    player_id,
    game_id,
    date,
    goals,
    team=None,
    tgf=None,
    season="20232024",
    name=None,
    position="C",
    pp=None,
):
    conn.execute(
        "INSERT INTO player_games (player_id, game_id, season, game_date, goals,"
        " team_abbrev, team_goals_for, name, position, power_play_goals)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (player_id, game_id, season, date, goals, team, tgf, name, position, pp),
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


def test_compute_derived_ppg_is_strictly_before_and_debut_is_zero(tmp_path):
    conn = local_store.connect(tmp_path / "raw.sqlite")
    _insert(conn, 201, 8001, "2023-10-10", 1, pp=1)
    _insert(conn, 201, 8002, "2023-10-12", 0, pp=0)
    _insert(conn, 201, 8003, "2023-10-14", 2, pp=1)

    local_store.compute_derived(conn, "20232024")
    rows = {r["game_date"]: r for r in conn.execute("SELECT * FROM derived_features")}

    # Debut: 0, not NULL - same pre-debut convention as gpg.
    assert rows["2023-10-10"]["ppg"] == 0
    # Power play goals strictly before each row, over games strictly before it.
    assert rows["2023-10-12"]["ppg"] == 1.0
    assert rows["2023-10-14"]["ppg"] == 0.5


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

    # No power play goals in the fixture, so every opponent's shorthanded-goals-
    # against entering is 0/1 = 0.0 - not NULL: only the first game has no rate.
    assert rows[("BOS", "2023-10-12")]["otshga"] == 0.0
    assert rows[("TOR", "2023-10-12")]["otshga"] == 0.0


def test_compute_derived_team_otshga_is_the_opponents_shorthanded_goals_against(tmp_path):
    conn = local_store.connect(tmp_path / "raw.sqlite")
    # Game 1: BOS scores one power play goal, TOR scores none.
    # Game 2: BOS scores two more, TOR still none.
    _insert(conn, 101, 9001, "2023-10-10", 1, team="BOS", tgf=3, pp=1)
    _insert(conn, 102, 9001, "2023-10-10", 2, team="TOR", tgf=5, pp=0)
    _insert(conn, 101, 9002, "2023-10-12", 0, team="BOS", tgf=4, pp=2)
    _insert(conn, 102, 9002, "2023-10-12", 1, team="TOR", tgf=2, pp=0)

    local_store.compute_derived_team(conn, "20232024")
    rows = {(r["team_abbrev"], r["game_date"]): r for r in conn.execute("SELECT * FROM derived_team_stats")}

    # First game of the season: no rate entering it on either side.
    assert rows[("BOS", "2023-10-10")]["otshga"] is None
    assert rows[("TOR", "2023-10-10")]["otshga"] is None

    # The value on a team's row is its OPPONENT's shorthanded goals against
    # entering, read off the opponent's row exactly like otga: TOR had allowed 1
    # (BOS's power play goal in game 1) over 1 game -> 1.0 on BOS's row...
    assert rows[("BOS", "2023-10-12")]["otshga"] == 1.0
    # ...and BOS had allowed 0 over 1 game -> 0.0, not NULL, on TOR's row.
    assert rows[("TOR", "2023-10-12")]["otshga"] == 0.0


def test_compute_derived_team_rejects_games_without_an_opponent(tmp_path):
    conn = local_store.connect(tmp_path / "raw.sqlite")
    _insert(conn, 101, 9001, "2023-10-10", 1, team="BOS", tgf=3)

    # Dropping the orphan silently would make otga wrong rather than absent.
    with pytest.raises(ValueError, match="no opponent"):
        local_store.compute_derived_team(conn, "20232024")


class _FakeArchiveClient:
    """Supabase stand-in: serves canned archive-name rows, captures upserts."""

    def __init__(self, archive_rows):
        self.archive_rows = archive_rows
        self.upserted = []
        self.table_names = []

    def table(self, name):
        self.table_names.append(name)
        return _FakeTable(self)


class _FakeTable:
    def __init__(self, client):
        self._client = client

    def select(self, *args, **kwargs):
        return _FakeSelect(self._client)

    def upsert(self, batch, **kwargs):
        self._client.upserted.extend(batch)
        return _FakeDone()


class _FakeSelect:
    def __init__(self, client, ids=()):
        self._client = client
        self._ids = ids

    def in_(self, column, ids):
        return _FakeSelect(self._client, ids)

    def execute(self):
        wanted = set(self._ids)
        rows = [r for r in self._client.archive_rows if r["player_id"] in wanted]
        return SimpleNamespace(data=rows)


class _FakeDone:
    def execute(self):
        return SimpleNamespace(data=None)


def test_publish_prefers_archive_names_over_boxscore_initials(tmp_path, monkeypatch):
    conn = local_store.connect(tmp_path / "raw.sqlite")
    _insert(conn, 201, 8001, "2023-10-10", 1, team="BOS", tgf=3, name="B. Burns")
    _insert(conn, 202, 8001, "2023-10-10", 0, team="BOS", tgf=3, name="J. Doe")
    local_store.compute_derived(conn, "20232024")
    conn.close()

    # A NULL name row first: it must not shadow the real one for the same player.
    client = _FakeArchiveClient(
        [
            {"player_id": 201, "name": None},
            {"player_id": 201, "name": "Brent Burns"},
        ]
    )
    monkeypatch.setattr(local_store, "config", lambda: ("dev", client))

    written = local_store.publish("20232024", db_path=tmp_path / "raw.sqlite")

    assert written == 2
    # Names were read from the archive table and rows written to the backtrack one.
    assert set(client.table_names) == {"Player-Snapshots-dev", "Player-Snapshots-backtrack-dev"}
    names = {r["player_id"]: r["name"] for r in client.upserted}
    # The archive's full spelling wins where it exists...
    assert names[201] == "Brent Burns"
    # ...and a player the archive never saw keeps the box-score fallback.
    assert names[202] == "J. Doe"
    assert {r["team_name"] for r in client.upserted} == {local_store.to_place("BOS")}
    # ppg rides every payload row - a column the target table must declare first
    # (20261009_add_ppg_player_snapshots_backtrack.sql), or the batch would fail.
    assert all("ppg" in r for r in client.upserted)
