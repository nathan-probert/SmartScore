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
    opponent=None,
):
    conn.execute(
        "INSERT INTO player_games (player_id, game_id, season, game_date, goals,"
        " team_abbrev, team_goals_for, name, position, power_play_goals, opponent_abbrev)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (player_id, game_id, season, date, goals, team, tgf, name, position, pp, opponent),
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


def test_build_merges_playoff_game_logs(tmp_path, monkeypatch):
    boxscore = {
        "player_id": 301,
        "game_id": 2023030401,
        "game_date": "2024-04-20",
        "name": "P. Layer",
        "position": "C",
        "sweater_number": "9",
        "team_abbrev": "BOS",
        "home": False,
        "team_goals_for": 3,
        "goals": 1,
        "assists": 0,
        "points": 1,
        "shots": 2,
        "pim": 0,
        "toi": "12:00",
        "plus_minus": 1,
        "shifts": 18,
        "power_play_goals": 0,
    }
    log_calls = []

    def fake_games(season, **kwargs):
        return [{"id": 2023030401, "date": "2024-04-20", "away": "TOR", "home": "BOS"}]

    def fake_boxscore(game_id, **kwargs):
        return [boxscore]

    def fake_log(player_id, season, game_type=2, **kwargs):
        log_calls.append(game_type)
        if game_type == 3:
            return [
                {
                    "gameId": 2023030401,
                    "gameDate": "2024-04-20",
                    "teamAbbrev": "BOS",
                    "opponentAbbrev": "TOR",
                    "homeRoadFlag": "H",
                    "goals": 1,
                    "assists": 0,
                    "points": 1,
                    "shots": 2,
                    "pim": 0,
                    "toi": "12:00",
                    "plusMinus": 1,
                    "shifts": 18,
                    "powerPlayGoals": 0,
                    "powerPlayPoints": 0,
                    "shorthandedGoals": 1,
                    "shorthandedPoints": 1,
                    "otGoals": 0,
                    "gameWinningGoals": 0,
                }
            ]
        return []

    monkeypatch.setattr(local_store, "season_games", fake_games)
    monkeypatch.setattr(local_store, "fetch_boxscore", fake_boxscore)
    monkeypatch.setattr(local_store, "fetch_game_log", fake_log)
    # No shift data in this fixture: the lineup pass runs but finds nothing.
    monkeypatch.setattr(local_store, "fetch_shiftcharts", lambda game_id, **kwargs: [])
    # The feed ingest runs at the end of build; stub both maps so it never
    # reaches the network, with just enough data to prove the tables fill.
    monkeypatch.setattr(
        local_store,
        "tims_map",
        lambda season, **kwargs: {"2024-04-20": {301: {"group": 1, "scored": True}}},
    )
    monkeypatch.setattr(
        local_store,
        "goalies_map",
        lambda season, **kwargs: {"2024-04-20": {"BOS": {"name": "J. Swayman", "status": "Confirmed"}}},
    )

    local_store.build("20232024", db_path=tmp_path / "raw.sqlite")

    conn = local_store.connect(tmp_path / "raw.sqlite")
    row = conn.execute("SELECT * FROM player_games WHERE game_id = 2023030401").fetchone()

    # The playoff game was found, its gameType-3 log fetched, and both feeds
    # merged onto the same row: box-score fields kept, gamelog fields filled.
    assert 3 in log_calls
    assert row["position"] == "C"
    assert row["shorthanded_goals"] == 1
    assert row["gamelog_fields"] is not None
    # The feed ingest wrote its tables alongside the raw rows.
    tims_row = conn.execute("SELECT * FROM tims_daily WHERE player_id = 301").fetchone()
    assert tims_row["group_id"] == 1 and tims_row["scored"] == 1
    goalie_row = conn.execute("SELECT * FROM goalies_daily WHERE team_abbrev = 'BOS'").fetchone()
    assert goalie_row["goalie_name"] == "J. Swayman" and goalie_row["status"] == "Confirmed"
    conn.close()


def test_ingest_challenge_feeds_rebuilds_the_season_rows(tmp_path, monkeypatch):
    conn = local_store.connect(tmp_path / "raw.sqlite")
    monkeypatch.setattr(
        local_store,
        "tims_map",
        lambda season, **kwargs: {"2023-10-10": {201: {"group": 2, "scored": False}}},
    )
    monkeypatch.setattr(
        local_store,
        "goalies_map",
        lambda season, **kwargs: {"2023-10-10": {"TOR": {"name": "I. Samsonov", "status": "Confirmed"}}},
    )

    local_store.ingest_challenge_feeds(conn, "20232024")
    # A second run is a delete-and-reinsert, not a duplicate.
    local_store.ingest_challenge_feeds(conn, "20232024")

    tims_rows = conn.execute("SELECT * FROM tims_daily").fetchall()
    assert len(tims_rows) == 1
    assert tims_rows[0]["season"] == "20232024"
    assert tims_rows[0]["group_id"] == 2
    assert tims_rows[0]["scored"] == 0

    goalie_rows = conn.execute("SELECT * FROM goalies_daily").fetchall()
    assert len(goalie_rows) == 1
    assert goalie_rows[0]["goalie_name"] == "I. Samsonov"
    assert goalie_rows[0]["status"] == "Confirmed"

    # Another season's rows survive a rebuild of this one: the delete is
    # scoped by season, so backfilled seasons cannot clobber each other.
    conn.execute(
        "INSERT INTO tims_daily (season, game_date, player_id, group_id) VALUES ('20222023', '2023-01-01', 999, 1)"
    )
    conn.commit()
    monkeypatch.setattr(local_store, "tims_map", lambda season, **kwargs: {})
    local_store.ingest_challenge_feeds(conn, "20232024")

    assert conn.execute("SELECT COUNT(*) FROM tims_daily WHERE season = '20222023'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM tims_daily WHERE season = '20232024'").fetchone()[0] == 0
    conn.close()


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
    _insert(conn, 201, 8001, "2023-10-10", 1, team="BOS", tgf=3, name="B. Burns", opponent="TOR")
    _insert(conn, 202, 8001, "2023-10-10", 0, team="BOS", tgf=3, name="J. Doe", opponent="TOR")
    local_store.compute_derived(conn, "20232024")
    # Feed rows seeded the way build/--feeds would leave them: one player in
    # tims group 2, and TOR's starter Samsonov (Confirmed) on the fixture's
    # date. BOS has no goalie entry, which only matters for the other side.
    conn.execute(
        "INSERT INTO tims_daily (season, game_date, player_id, group_id, scored)"
        " VALUES ('20232024', '2023-10-10', 201, 2, 1)"
    )
    conn.execute(
        "INSERT INTO goalies_daily (season, game_date, team_abbrev, goalie_name, status)"
        " VALUES ('20232024', '2023-10-10', 'TOR', 'Ilya Samsonov', 'Confirmed')"
    )
    conn.commit()
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
    # tims from the history map; lineup columns present (NULL without shifts).
    tims = {r["player_id"]: r["tims"] for r in client.upserted}
    assert tims == {201: 2, 202: 0}
    assert all("lineup_unit" in r and "pp_unit" in r and "lineup_status" in r for r in client.upserted)
    # Training label from the fixture's goals column: 201 scored, 202 did not.
    assert {r["player_id"]: r["scored"] for r in client.upserted} == {201: 1, 202: 0}
    # Opponent starter denormalized per the live schema: identity, upper-cased
    # status, separate confirmed boolean. Stat columns are never sent - they
    # stay NULL rather than shipping season-end totals as an October view.
    assert all(
        r["opp_goalie_name"] == "Ilya Samsonov"
        and r["opp_goalie_team"] == "TOR"
        and r["opp_goalie_status"] == "CONFIRMED"
        and r["opp_goalie_confirmed"] is True
        for r in client.upserted
    )
    assert all("opp_goalie_gaa" not in r for r in client.upserted)


def _shift(player_id, team_id, start, end, period=1):
    """One synthetic shiftchart record; times are M:SS game-clock marks."""
    return {
        "playerId": player_id,
        "teamId": team_id,
        "period": period,
        "startTime": start,
        "endTime": end,
    }


def test_compute_lineups_reconstructs_lines_and_pp_units(tmp_path, monkeypatch):
    conn = local_store.connect(tmp_path / "raw.sqlite")

    # Home (team 100): line1 = 1,2,3; line2 = 4,5,6. D pair = 7,8.
    # Away (team 200): line1 = 30,31,32; line2 = 33,34,35. D pair = 42,43.
    # PP window 01:00-03:00: home {1,2,3,4,7} (5) vs away {30,31,32,42} (4).
    shifts = [
        _shift(1, 100, "00:00", "10:00"),
        _shift(2, 100, "00:00", "10:00"),
        _shift(3, 100, "00:00", "10:00"),
        _shift(4, 100, "01:00", "03:00"),  # PP-only stint for F4
        _shift(4, 100, "10:00", "16:00"),
        _shift(5, 100, "10:00", "16:00"),
        _shift(6, 100, "10:00", "16:00"),
        _shift(7, 100, "00:00", "10:00"),
        _shift(8, 100, "03:00", "10:00"),
        _shift(30, 200, "00:00", "10:00"),
        _shift(31, 200, "00:00", "10:00"),
        _shift(32, 200, "00:00", "10:00"),
        _shift(33, 200, "10:00", "16:00"),
        _shift(34, 200, "10:00", "16:00"),
        _shift(35, 200, "10:00", "16:00"),
        _shift(42, 200, "00:00", "10:00"),
        _shift(43, 200, "03:00", "10:00"),
    ]

    positions = {
        1: "C",
        2: "L",
        3: "R",
        4: "C",
        5: "L",
        6: "R",
        7: "D",
        8: "D",
        30: "C",
        31: "L",
        32: "R",
        33: "C",
        34: "L",
        35: "R",
        42: "D",
        43: "D",
    }

    for player_id, position in positions.items():
        _insert(conn, player_id, 7001, "2023-10-10", 0, team="BOS" if player_id < 100 else "TOR", position=position)

    monkeypatch.setattr(local_store, "fetch_shiftcharts", lambda game_id, **kwargs: shifts)
    local_store.compute_lineups(conn, "20232024")

    rows = {r["player_id"]: r for r in conn.execute("SELECT * FROM player_lineups")}
    conn.close()

    # Line 1 (1,2,3) carries the PP minutes so it ranks above line 2 (4,5,6).
    assert rows[1]["lineup_unit"] == rows[2]["lineup_unit"] == rows[3]["lineup_unit"] == "F1"
    assert rows[4]["lineup_unit"] == rows[5]["lineup_unit"] == rows[6]["lineup_unit"] == "F2"
    assert rows[4]["lineup_unit"] != rows[1]["lineup_unit"]
    # D pair 7,8 (7 also on the PP) ranks first among pairs.
    assert rows[7]["lineup_unit"] == rows[8]["lineup_unit"] == "D1"
    # The five-man PP unit gets PP1; the second even line gets no PP time at all.
    assert rows[1]["pp_unit"] == rows[2]["pp_unit"] == rows[3]["pp_unit"] == "PP1"
    assert rows[4]["pp_unit"] == "PP1" and rows[7]["pp_unit"] == "PP1"
    assert rows[5]["pp_unit"] is None and rows[6]["pp_unit"] is None
    # Shorthanded time is not power-play time: away's PK trio has no pp_unit.
    assert rows[30]["pp_unit"] is None and rows[42]["pp_unit"] is None
    assert rows[30]["lineup_unit"] == rows[31]["lineup_unit"] == rows[32]["lineup_unit"] == "F1"
    # Every matched skater is ACTUAL, position-grouped by role.
    assert rows[1]["lineup_status"] == "ACTUAL" and rows[1]["lineup_position_group"] == "F"
    assert rows[7]["lineup_position_group"] == "D"
