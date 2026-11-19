"""Unit tests for the local ML history store (ml_history_store.py): the
SQLite twin of the backtrack family - schema/upsert semantics (the lane
split's "unmentioned columns keep their value" contract), the marker
shelf, the read helpers the daily run and the training path consume, and
the ML_HISTORY_BACKEND switch (default supabase = CI unchanged)."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "smartscore"))
sys.path.insert(1, str(ROOT / "smartscore" / "scripts" / "backtrack"))

_spec = importlib.util.spec_from_file_location(
    "ml_history_store", ROOT / "smartscore" / "scripts" / "backtrack" / "ml_history_store.py"
)
mls = importlib.util.module_from_spec(_spec)
sys.modules["ml_history_store"] = mls
_spec.loader.exec_module(mls)

PLAYER_TABLE = "Player-Snapshots-backtrack-prod"
TEAM_TABLE = "Team-Stats-backtrack-prod"
MARKER_TABLE = "Player-Game-Raw-Markers-prod"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fresh per-test SQLite store + the sqlite backend forced on."""
    monkeypatch.setenv("ML_HISTORY_BACKEND", "sqlite")
    monkeypatch.setenv("ML_HISTORY_DB", str(tmp_path / "ml_history.sqlite"))
    monkeypatch.delenv("ML_HISTORY_DUAL_WRITE", raising=False)
    return mls


@pytest.fixture
def seeded_player(store):
    """One fully-published row: a sentinel on a PRECOMPUTED rolling column
    that any wrongly-scoped upsert would clobber."""
    mls.upsert_rows(
        PLAYER_TABLE,
        [
            {
                "date": "2026-10-25",
                "player_id": 8478402,
                "name": "Connor McDavid",
                "team_name": "Edmonton",
                "home": True,
                "scored": 1,
                "gpg": 0.5,
                "r5_shots_pg": 3.2,
                "game_shots": 4,
            }
        ],
    )
    return "2026-10-25"


# ------------------------------------------------------------------ upserts


def test_create_lane_inserts_and_preserves_sentinel(store, seeded_player):
    """The backfill's UPDATE lane (raw cols only) must not touch the
    published rolling columns - the PostgREST explicit-NULL hazard the lane
    split exists for."""
    mls.upsert_rows(
        PLAYER_TABLE,
        [{"date": "2026-10-25", "player_id": 8478402, "game_shots": 7, "game_goals": 1}],
    )
    frame = mls.fetch_window(
        PLAYER_TABLE, ["game_shots", "game_goals", "r5_shots_pg", "gpg", "name"], "2026-10-25", "2026-10-26", ["date"]
    )
    row = frame.iloc[0]
    assert int(row["game_shots"]) == 7
    assert int(row["game_goals"]) == 1
    assert float(row["r5_shots_pg"]) == 3.2  # sentinel intact
    assert float(row["gpg"]) == 0.5
    assert row["name"] == "Connor McDavid"


def test_create_lane_adds_absent_row(store):
    mls.upsert_rows(PLAYER_TABLE, [{"date": "2026-10-26", "player_id": 8476953, "name": "Leon Draisaitl"}])
    assert mls.count(PLAYER_TABLE) == 1
    assert mls.existing_player_keys(PLAYER_TABLE, "2026-10-26") == {8476953}


def test_upsert_idempotent_by_pk(store):
    mls.upsert_rows(
        PLAYER_TABLE, [{"date": "2026-10-25", "player_id": 8478402, "name": "Connor McDavid", "game_shots": 4}]
    )
    mls.upsert_rows(
        PLAYER_TABLE, [{"date": "2026-10-25", "player_id": 8478402, "name": "Connor McDavid", "game_shots": 6}]
    )
    assert mls.count(PLAYER_TABLE) == 1


def test_upsert_mixed_shapes_one_call(store):
    """Unlike the PostgREST caller (which must pre-split lanes), the local
    upsert accepts mixed row shapes in one call."""
    mls.upsert_rows(
        PLAYER_TABLE,
        [
            {"date": "2026-10-25", "player_id": 8478402, "name": "Connor McDavid", "game_shots": 4},
            {"date": "2026-10-25", "player_id": 8478402, "game_shots": 5, "game_goals": 1},
        ],
    )
    frame = mls.fetch_window(PLAYER_TABLE, ["game_shots", "game_goals"], "2026-10-25", "2026-10-26", ["date"])
    assert int(frame.iloc[0]["game_shots"]) == 5
    assert int(frame.iloc[0]["game_goals"]) == 1


def test_unknown_table_raises(store):
    with pytest.raises(RuntimeError, match="not part of the backtrack family"):
        mls.upsert_rows("Picks-prod", [{"id": 1}])


# -------------------------------------------------------------- marker shelf


def test_marker_upsert_stamps_and_idempotent(store):
    mls.marker_upsert(MARKER_TABLE, {"date": "2026-10-25", "games": 3, "player_games": 61})
    mls.marker_upsert(MARKER_TABLE, {"date": "2026-10-25", "games": 3, "player_games": 61})
    assert mls.count(MARKER_TABLE) == 1
    assert mls.marked_dates(MARKER_TABLE, "2026-10-01") == {"2026-10-25"}


def test_marked_dates_missing_store_is_empty(store, monkeypatch):
    monkeypatch.setenv("ML_HISTORY_DB", "/nonexistent/dir/ml_history.sqlite")
    assert mls.marked_dates(MARKER_TABLE, "2026-10-01") == set()


# ------------------------------------------------------------------- reads


def test_fetch_window_half_open_and_ordered(store, seeded_player):
    mls.upsert_rows(
        PLAYER_TABLE,
        [
            {"date": "2026-10-25", "player_id": 8476953, "name": "Leon Draisaitl"},
            {"date": "2026-10-26", "player_id": 8478402, "name": "Connor McDavid"},
        ],
    )
    frame = mls.fetch_window(
        PLAYER_TABLE, ["date", "player_id", "name"], "2026-10-01", "2026-10-26", ["date", "player_id"]
    )
    # hi is EXCLUSIVE: the pick date's own post-game row must not leak in.
    # ORDER BY (date, player_id): same-date rows sort by player_id.
    assert list(frame["player_id"]) == [8476953, 8478402]
    assert "2026-10-26" not in set(frame["date"])


def test_first_and_latest_date(store, seeded_player):
    mls.upsert_rows(PLAYER_TABLE, [{"date": "2026-10-20", "player_id": 8476953, "name": "Leon Draisaitl"}])
    assert mls.first_date(PLAYER_TABLE) == "2026-10-20"
    assert mls.latest_date(PLAYER_TABLE) == "2026-10-25"


def test_latest_date_empty_is_none(store):
    assert mls.latest_date(PLAYER_TABLE) is None


def test_latest_rows_per_player_takes_tail(store):
    mls.upsert_rows(
        PLAYER_TABLE,
        [
            {"date": "2026-10-20", "player_id": 8478402, "name": "Connor McDavid", "r5_shots_pg": 2.0},
            {"date": "2026-10-25", "player_id": 8478402, "name": "Connor McDavid", "r5_shots_pg": 3.2},
            {"date": "2026-10-25", "player_id": 8476953, "name": "Leon Draisaitl", "r5_shots_pg": 4.1},
        ],
    )
    frame = mls.latest_rows_per_player(PLAYER_TABLE, ["date", "player_id", "r5_shots_pg"], [8478402, 8476953])
    assert len(frame) == 2
    mcdavid = frame[frame["player_id"] == 8478402].iloc[0]
    assert float(mcdavid["r5_shots_pg"]) == 3.2  # the LATEST row, not the first


def test_read_on_unbootstrapped_store_fails_loud(store, monkeypatch):
    monkeypatch.setenv("ML_HISTORY_DB", "/nonexistent/dir/ml_history.sqlite")
    with pytest.raises(RuntimeError, match="bootstrap it first"):
        mls.first_date(PLAYER_TABLE)


# --------------------------------------------------------- dual-write mirror


def test_dual_write_mirrors_and_survives_mirror_failure(store, monkeypatch, seeded_player):
    monkeypatch.setenv("ML_HISTORY_DUAL_WRITE", "1")
    fake_client = MagicMock(name="supabase_admin")
    fake_client.table.return_value.upsert.return_value.execute.side_effect = RuntimeError("db full")
    monkeypatch.setattr(mls, "SUPABASE_ADMIN_CLIENT", fake_client)
    # must NOT raise: the mirror is loud-continue, SQLite stays committed
    written = mls.upsert_rows(PLAYER_TABLE, [{"date": "2026-10-25", "player_id": 8478402, "game_shots": 9}])
    assert written == 1
    frame = mls.fetch_window(PLAYER_TABLE, ["game_shots"], "2026-10-25", "2026-10-26", ["date"])
    assert int(frame.iloc[0]["game_shots"]) == 9


def test_dual_write_off_touches_nothing_remote(store, monkeypatch, seeded_player):
    fake_client = MagicMock(name="supabase_admin")
    monkeypatch.setattr(mls, "SUPABASE_ADMIN_CLIENT", fake_client)
    mls.upsert_rows(PLAYER_TABLE, [{"date": "2026-10-25", "player_id": 8478402, "game_shots": 9}])
    fake_client.table.assert_not_called()


# ------------------------------------------------------------- backend switch


def test_backend_defaults_to_supabase(monkeypatch):
    monkeypatch.delenv("ML_HISTORY_BACKEND", raising=False)
    assert mls.backend() == "supabase"


def test_backend_sqlite_via_env(store):
    assert mls.backend() == "sqlite"


def test_team_and_goalie_tables_have_their_own_pks(store):
    mls.upsert_rows(
        TEAM_TABLE, [{"season": "20262027", "team_abbrev": "TOR", "game_id": 1, "date": "2026-10-25", "tgpg": 3.75}]
    )
    mls.upsert_rows(
        TEAM_TABLE,
        [{"season": "20262027", "team_abbrev": "TOR", "game_id": 1, "date": "2026-10-25", "tgpg": 3.4, "otga": 2.9}],
    )
    assert mls.count(TEAM_TABLE) == 1
    frame = mls.fetch_window(TEAM_TABLE, ["tgpg", "otga"], "2026-01-01", "2027-01-01", ["date"])
    assert float(frame.iloc[0]["tgpg"]) == 3.4


# ------------------------------------------- backfill routing (integration)


def test_backfill_routes_to_sqlite(store):
    """The gap-fill helper's reads/writes honor the backend switch: with
    ML_HISTORY_BACKEND=sqlite nothing touches the (mocked) Supabase client,
    an un-bootstrapped store reads as the empty-ledger state (None), and
    the raw-column probe is constant True."""
    spec = importlib.util.spec_from_file_location(
        "backfill_under_store", ROOT / "smartscore" / "scripts" / "backtrack" / "backfill_missing_days.py"
    )
    bf = importlib.util.module_from_spec(spec)
    sys.modules["backfill_under_store"] = bf
    spec.loader.exec_module(bf)

    fake_client = MagicMock(name="supabase_admin")
    bf.SUPABASE_ADMIN_CLIENT = fake_client
    try:
        assert bf.latest_published_date("prod") is None  # empty local ledger, no Supabase read
        assert bf.raw_columns_landed(bf.player_table("prod")) is True
        assert bf.marked_dates("prod", "2026-10-01") == set()
        fake_client.table.assert_not_called()
    finally:
        bf.SUPABASE_ADMIN_CLIENT = None
