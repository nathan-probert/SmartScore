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


def test_compute_historic_spans_three_seasons_and_is_strictly_before(tmp_path):
    """hgpg/hppg = window totals / window games, current season strictly before the row."""
    conn = local_store.connect(tmp_path / "raw.sqlite")
    # Prior seasons, complete: 3 goals (2 PP) in 2 games of 20212022...
    _insert(conn, 201, 7001, "2021-11-01", 1, season="20212022", pp=1)
    _insert(conn, 201, 7002, "2022-01-15", 2, season="20212022", pp=1)
    # ...plus 1 goal (0 PP) in 1 game of 20222023: window so far 4 goals, 2 pp, 3 games.
    _insert(conn, 201, 7501, "2022-10-20", 1, season="20222023", pp=0)
    # Current season: strictly-before frame adds each earlier 20232024 game only.
    _insert(conn, 201, 8001, "2023-10-10", 2, season="20232024", pp=1)
    _insert(conn, 201, 8002, "2023-10-12", 1, season="20232024", pp=0)

    local_store.compute_derived(conn, "20232024")
    rows = {r["game_date"]: r for r in conn.execute("SELECT * FROM derived_features")}

    # Season debut: the window is the two prior seasons only (no current-season
    # games strictly before it).
    assert rows["2023-10-10"]["hgpg"] == pytest.approx(4 / 3)
    assert rows["2023-10-10"]["hppg"] == pytest.approx(2 / 3)
    # Next game: the debut joins the window (goals 4+2=6, pp 2+1=3, games 3+1=4),
    # while its own game stays out - strictly-before, like gpg.
    assert rows["2023-10-12"]["hgpg"] == pytest.approx(6 / 4)
    assert rows["2023-10-12"]["hppg"] == pytest.approx(3 / 4)


def test_compute_historic_is_null_until_the_full_window_exists(tmp_path):
    """A partial window would divide by a missing denominator - NULL means 'unavailable'."""
    conn = local_store.connect(tmp_path / "raw.sqlite")
    _insert(conn, 201, 7501, "2022-10-20", 1, season="20222023")
    _insert(conn, 201, 8001, "2023-10-10", 1, season="20232024")

    local_store.compute_derived(conn, "20232024")
    row = conn.execute("SELECT hgpg, hppg FROM derived_features").fetchone()

    # 20212022 was never crawled, so a "3-year" rate here would silently be a
    # 2-year one - stay null instead.
    assert row["hgpg"] is None
    assert row["hppg"] is None


def test_compute_historic_pre_debut_window_reads_zero(tmp_path):
    """Window exists but has no games for this player: 0/0 -> 0, the archive's convention."""
    conn = local_store.connect(tmp_path / "raw.sqlite")
    # Both prior seasons crawled, but only another player appears in them...
    _insert(conn, 999, 7001, "2021-11-01", 1, season="20212022")
    _insert(conn, 999, 7501, "2022-10-20", 1, season="20222023")
    # ...so this player's debut has an empty window: 0 games, not "unavailable".
    _insert(conn, 201, 8001, "2023-10-10", 2, season="20232024")

    local_store.compute_derived(conn, "20232024")
    row = conn.execute("SELECT hgpg, hppg FROM derived_features").fetchone()

    assert row["hgpg"] == 0
    assert row["hppg"] == 0


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
    # hgpg/hppg likewise (20261008 declares them). The fixture has no prior
    # seasons, so the values are NULL - the payload must still carry the keys.
    assert all("hgpg" in r and "hppg" in r for r in client.upserted)
    # tims from the history map; lineup columns present (NULL without shifts).
    tims = {r["player_id"]: r["tims"] for r in client.upserted}
    assert tims == {201: 2, 202: 0}
    assert all("lineup_unit" in r and "pp_unit" in r and "lineup_status" in r for r in client.upserted)
    # Training label from the fixture's goals column: 201 scored, 202 did not.
    assert {r["player_id"]: r["scored"] for r in client.upserted} == {201: 1, 202: 0}
    # Opponent starter denormalized per the live schema: identity, upper-cased
    # status, separate confirmed boolean. Stat keys are present on every payload
    # row; this fixture has no goalie appearance rows, so the values are NULL.
    assert all(
        r["opp_goalie_name"] == "Ilya Samsonov"
        and r["opp_goalie_team"] == "TOR"
        and r["opp_goalie_status"] == "CONFIRMED"
        and r["opp_goalie_confirmed"] is True
        for r in client.upserted
    )
    assert all(
        r["opp_goalie_gaa"] is None
        and r["opp_goalie_save_pct"] is None
        and r["opp_goalie_record"] is None
        and r["opp_goalie_shutouts"] is None
        and r["opp_goalie_games_played"] is None
        and r["opp_goalie_nhl_id"] is None
        for r in client.upserted
    )


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


def _insert_goalie(  # noqa: PLR0913, PLR0917 - helper mirrors the table's shape; every extra has a default
    conn,
    player_id,
    game_id,
    date,
    ga,
    sa,
    toi,
    decision,
    team,
    shutouts=0,
    season="20232024",
    name=None,
    saves=None,
):
    """One goalie appearance row: game-log-only columns, like a merged build.

    ``saves`` defaults to ``sa - ga`` (the usual case); pass an explicit value
    to exercise club-stats' saves-based quirk where official saves exceed it.
    """
    if saves is None:
        saves = sa - ga
    conn.execute(
        "INSERT INTO player_games (player_id, game_id, season, game_date, position,"
        " team_abbrev, name, toi, goals_against, shots_against, saves, shutouts, decision, games_started)"
        " VALUES (?, ?, ?, ?, 'G', ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (player_id, game_id, season, date, team, name, toi, ga, sa, saves, shutouts, decision),
    )


def test_merge_gamelog_maps_goalie_fields():
    row = local_store._merge_gamelog(  # noqa: SLF001 - unit-testing the mapping itself
        {
            "gameId": 2023020001,
            "gameDate": "2023-10-10",
            "teamAbbrev": "TBL",
            "opponentAbbrev": "DET",
            "homeRoadFlag": "H",
            "goals": 0,
            "assists": 0,
            "toi": "59:12",
            "gamesStarted": 1,
            "decision": "W",
            "shotsAgainst": 28,
            "goalsAgainst": 1,
            "savePctg": 0.964286,
            "shutouts": 0,
        },
        "20232024",
    )

    assert row["goals_against"] == 1
    assert row["shots_against"] == 28
    # Official saves = round(savePctg * shots) = round(0.964286 * 28) = 27;
    # equals sa - ga here, but the derivation is what club-stats sums.
    assert row["saves"] == 27
    assert row["shutouts"] == 0
    assert row["decision"] == "W"
    assert row["games_started"] == 1


def test_merge_gamelog_saves_round_trips_official_saves():
    """savePctg * shots reproduces official saves even when they exceed sa - ga."""
    row = local_store._merge_gamelog(  # noqa: SLF001 - unit-testing the mapping itself
        {
            "gameId": 2023020001,
            "gameDate": "2023-10-10",
            "teamAbbrev": "ANA",
            "shotsAgainst": 44,
            "goalsAgainst": 5,
            "savePctg": 0.909091,  # 40/44 - official saves 40, sa - ga = 39
        },
        "20232024",
    )
    assert row["saves"] == 40

    # No shots / no pct: NULL, and a skater entry has neither.
    empty = local_store._merge_gamelog({"gameId": 2023020002, "goals": 1}, "20232024")  # noqa: SLF001
    assert empty["saves"] is None


def test_compute_goalie_features_is_strictly_before(tmp_path):
    conn = local_store.connect(tmp_path / "raw.sqlite")
    # Two full games then a third: strictly-before frames of 0, then 1, then 2.
    # 60:00 each, 2 GA / 30 SA, then 3 GA / 24 SA, then 1 GA / 20 SA.
    _insert_goalie(conn, 9001, 90001, "2023-10-10", 2, 30, "60:00", "W", "TBL")
    _insert_goalie(conn, 9001, 90002, "2023-10-12", 3, 24, "60:00", "L", "TBL")
    _insert_goalie(conn, 9001, 90003, "2023-10-14", 1, 20, "60:00", "O", "TBL", shutouts=1)

    local_store.compute_goalie_features(conn, "20232024")
    rows = {r["game_date"]: r for r in conn.execute("SELECT * FROM goalie_features")}
    conn.close()

    # Club debut: NULL, not 0 - club-stats does not list him yet.
    assert rows["2023-10-10"]["gaa"] is None
    assert rows["2023-10-10"]["save_pct"] is None
    assert rows["2023-10-10"]["record"] is None
    assert rows["2023-10-10"]["shutouts"] is None
    assert rows["2023-10-10"]["games_played"] is None

    # After one game: per-60 GAA = 3600 * 2 / 3600 = 2.0; SV = 28/30.
    assert rows["2023-10-12"]["gaa"] == pytest.approx(2.0)
    assert rows["2023-10-12"]["save_pct"] == pytest.approx(28 / 30)
    assert rows["2023-10-12"]["record"] == "1-0-0"
    assert rows["2023-10-12"]["shutouts"] == 0
    assert rows["2023-10-12"]["games_played"] == 1

    # After two: 5 GA over 120 min = 2.5; saves (54-5)/54; W-L-O 1-1-0.
    assert rows["2023-10-14"]["gaa"] == pytest.approx(2.5)
    assert rows["2023-10-14"]["save_pct"] == pytest.approx(49 / 54)
    assert rows["2023-10-14"]["record"] == "1-1-0"
    assert rows["2023-10-14"]["shutouts"] == 0
    assert rows["2023-10-14"]["games_played"] == 2


def test_compute_goalie_features_uses_official_saves(tmp_path):
    """save_pct is saves/shots, not (sa-ga)/sa: club-stats sums its saves field.

    Official saves can exceed sa - ga (Stolarz 2022-23: 444 vs 443), and
    savePercentage is saves/shots - the sa - ga shortcut reads low and fails
    the club-stats validation.
    """
    conn = local_store.connect(tmp_path / "raw.sqlite")
    _insert_goalie(conn, 9001, 90001, "2023-10-10", 2, 30, "60:00", "W", "TBL", saves=29)
    _insert_goalie(conn, 9001, 90002, "2023-10-12", 3, 24, "60:00", "L", "TBL")
    _insert_goalie(conn, 9001, 90003, "2023-10-14", 1, 20, "60:00", "W", "TBL")

    local_store.compute_goalie_features(conn, "20232024")
    rows = {r["game_date"]: r for r in conn.execute("SELECT * FROM goalie_features")}
    conn.close()

    # Window of one: saves 29 over 30 shots - NOT 28/30.
    assert rows["2023-10-12"]["save_pct"] == pytest.approx(29 / 30)
    # Window of two: saves 29 + 21 over 54 - NOT 49/54.
    assert rows["2023-10-14"]["save_pct"] == pytest.approx(50 / 54)


def test_compute_goalie_features_refuses_unmerged_saves(tmp_path):
    """A row with shots but no merged saves would silently read sv% 0."""
    conn = local_store.connect(tmp_path / "raw.sqlite")
    conn.execute(
        "INSERT INTO player_games (player_id, game_id, season, game_date, position,"
        " team_abbrev, toi, goals_against, shots_against) VALUES"
        " (9001, 90001, '20232024', '2023-10-10', 'G', 'TBL', '60:00', 2, 30)"
    )
    conn.commit()

    with pytest.raises(RuntimeError, match="no saves"):
        local_store.compute_goalie_features(conn, "20232024")
    conn.close()


def test_compute_goalie_features_zero_shots_reads_zero_not_null(tmp_path):
    """Post-debut 0-shot window: club-stats says savePercentage 0.0, sa 0.

    Seen in 2022-2023 TOR: a 1:10 stint with no shots against. NULL stays
    exclusive to a club debut, where the goalie is not listed at all.
    """
    conn = local_store.connect(tmp_path / "raw.sqlite")
    _insert_goalie(conn, 9001, 90001, "2023-10-10", 0, 0, "01:10", None, "TOR")
    _insert_goalie(conn, 9001, 90002, "2023-10-12", 2, 30, "60:00", "L", "TOR")

    local_store.compute_goalie_features(conn, "20232024")
    rows = {r["game_date"]: r for r in conn.execute("SELECT * FROM goalie_features")}
    conn.close()

    # Debut: still NULL - not listed yet.
    assert rows["2023-10-10"]["save_pct"] is None
    # After the 0-shot game: listed, 0 saves over 0 shots -> 0.0, not NULL.
    assert rows["2023-10-12"]["save_pct"] == 0.0
    # And the 0-goal/70-second stint reads a perfect 0.0 GAA, as theirs does.
    assert rows["2023-10-12"]["gaa"] == 0.0


def test_compute_goalie_features_resets_on_a_trade(tmp_path):
    """A mid-season trade restarts the window: club-stats is team-scoped."""
    conn = local_store.connect(tmp_path / "raw.sqlite")
    _insert_goalie(conn, 9001, 90001, "2023-10-10", 4, 20, "60:00", "L", "SJS")
    _insert_goalie(conn, 9001, 90002, "2023-10-12", 1, 30, "60:00", "W", "SJS")
    # Traded to COL: the COL window starts empty even though SJS games exist.
    _insert_goalie(conn, 9001, 90003, "2023-11-01", 2, 28, "60:00", "W", "COL")
    _insert_goalie(conn, 9001, 90004, "2023-11-03", 0, 25, "60:00", "W", "COL", shutouts=1)

    local_store.compute_goalie_features(conn, "20232024")
    rows = {(r["team_abbrev"], r["game_date"]): r for r in conn.execute("SELECT * FROM goalie_features")}
    conn.close()

    # COL debut is a club debut: NULL even though he played for SJS already.
    assert rows[("COL", "2023-11-01")]["gaa"] is None
    assert rows[("COL", "2023-11-01")]["games_played"] is None
    # Second COL game sees only the first COL game: 2 GA / 60 min, not 6/180.
    assert rows[("COL", "2023-11-03")]["gaa"] == pytest.approx(2.0)
    assert rows[("COL", "2023-11-03")]["record"] == "1-0-0"
    assert rows[("COL", "2023-11-03")]["shutouts"] == 0
    # SJS side accumulated independently.
    assert rows[("SJS", "2023-10-12")]["gaa"] == pytest.approx(3600.0 * 4 / 3600)
    assert rows[("SJS", "2023-10-12")]["record"] == "0-1-0"


def test_compute_goalie_features_skips_dressed_backups(tmp_path):
    """A box-score-only row (toi 00:00, no game-log stats) is not an appearance."""
    conn = local_store.connect(tmp_path / "raw.sqlite")
    _insert_goalie(conn, 9001, 90001, "2023-10-10", 2, 30, "60:00", "L", "TBL", name="J. Doe")
    # Backup dressed, never played: goals_against stays NULL.
    conn.execute(
        "INSERT INTO player_games (player_id, game_id, season, game_date, position,"
        " team_abbrev, name, toi) VALUES (9002, 90001, '20232024', '2023-10-10', 'G', 'TBL', 'R. Backup', '00:00')"
    )

    local_store.compute_goalie_features(conn, "20232024")
    rows = list(conn.execute("SELECT * FROM goalie_features"))
    conn.close()

    assert len(rows) == 1
    assert rows[0]["player_id"] == 9001


def test_match_goalie_resolves_full_name_to_box_initial():
    candidates = [(100, "J. Johansson"), (101, "A. Vasilevskiy")]
    assert local_store._match_goalie("Jonas Johansson", candidates) == 100  # noqa: SLF001
    assert local_store._match_goalie("Andrei Vasilevskiy", candidates) == 101  # noqa: SLF001
    # No last-name match, no first-initial match, empty name: refuse.
    assert local_store._match_goalie("Someone Else", candidates) is None  # noqa: SLF001
    assert local_store._match_goalie("Jonas Vasilevskiy", candidates) is None  # noqa: SLF001
    assert local_store._match_goalie("", candidates) is None  # noqa: SLF001
    # Ambiguity: two candidates, refuse rather than guess.
    assert local_store._match_goalie("Jonas Johansson", [(100, "J. Johansson"), (102, "J. Johansson")]) is None  # noqa: SLF001


def test_publish_attaches_goalie_stats_via_name_match(tmp_path, monkeypatch):
    conn = local_store.connect(tmp_path / "raw.sqlite")
    # Skater facing TOR on 2023-10-12.
    _insert(conn, 201, 8002, "2023-10-12", 1, team="BOS", tgf=3, name="B. Burns", opponent="TOR")
    # TOR's goalie Samsonov: two prior appearances, then the game being picked.
    _insert_goalie(conn, 9001, 90010, "2023-10-08", 2, 25, "60:00", "W", "TOR", name="I. Samsonov")
    _insert_goalie(conn, 9001, 90011, "2023-10-10", 1, 30, "60:00", "L", "TOR", name="I. Samsonov")
    _insert_goalie(conn, 9001, 90012, "2023-10-12", 3, 20, "60:00", "L", "TOR", name="I. Samsonov")
    local_store.compute_derived(conn, "20232024")
    conn.execute(
        "INSERT INTO goalies_daily (season, game_date, team_abbrev, goalie_name, status)"
        " VALUES ('20232024', '2023-10-12', 'TOR', 'Ilya Samsonov', 'Confirmed')"
    )
    conn.commit()
    conn.close()

    client = _FakeArchiveClient([])
    monkeypatch.setattr(local_store, "config", lambda: ("dev", client))
    local_store.publish("20232024", db_path=tmp_path / "raw.sqlite")

    (row,) = client.upserted
    # As of 2023-10-12, entering that game: two prior, 3 GA / 120 min = 1.5 GAA.
    assert row["opp_goalie_nhl_id"] == 9001
    assert row["opp_goalie_gaa"] == pytest.approx(1.5)
    assert row["opp_goalie_save_pct"] == pytest.approx(52 / 55)
    assert row["opp_goalie_record"] == "1-1-0"
    assert row["opp_goalie_shutouts"] == 0
    assert row["opp_goalie_games_played"] == 2
    assert row["opp_goalie_name"] == "Ilya Samsonov"
    assert row["opp_goalie_status"] == "CONFIRMED"
