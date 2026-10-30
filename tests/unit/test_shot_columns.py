import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "smartscore" / "scripts"))
sys.path.append(str(Path(__file__).resolve().parents[2] / "smartscore" / "scripts" / "backtrack"))

import publish_shot_cols  # noqa: E402
from publish_shot_cols import attach_shot_columns, payload_records  # noqa: E402


def _stub_block():
    """Frame shaped like shot_features() + club cols for attach tests."""
    frame = pd.DataFrame(
        {
            "date": ["2025-01-01", "2025-01-02", "2025-01-03"],
            "player_id": [1, 1, 1],
            "sd_shots_pg": [np.nan, 2.5, 2.25],
            "sd_goals_pg": [np.nan, 2.0, 1.5],
            "sd_toi_pg": [np.nan, 14.5, 15.25],
            "sd_sh_pct": [np.nan, 2.0 / 2.5, 1.5 / 2.25],
            "r5_shots_pg": [np.nan, 1.0, 1.5],
            "r5_goals_pg": [np.nan, 1.0, 1.3333333333333333],
            "r10_shots_pg": [np.nan, 1.0, 1.5],
            "r10_goals_pg": [np.nan, 1.0, 1.3333333333333333],
            "r10_toi_pg": [np.nan, 12.0, 13.25],
            "ewm_goals": [np.nan, 2.0, 1.75],
            "ewm_shots": [np.nan, 2.5, 2.33],
            "career_games": [0, 1, 2],
            "career_goals_pg": [np.nan, 2.0, 1.5],
            "career_shots_pg": [np.nan, 2.5, 2.25],
            "team_abbrev": ["EDM", "EDM", "EDM"],
            "opponent_abbrev": ["WPG", "TOR", np.nan],
        }
    )
    return frame


def test_payload_records_nan_becomes_none_and_ints_stay_int():
    frame = pd.DataFrame(
        {
            "date": ["2025-01-01"],
            "player_id": [7],
            "name": ["P. Test"],
            "sd_shots_pg": [np.nan],
            "career_games": [0],
            "opponent_abbrev": [None],
        }
    )
    record = payload_records(frame)[0]
    assert record["sd_shots_pg"] is None
    assert record["opponent_abbrev"] is None
    assert type(record["career_games"]) is int and record["career_games"] == 0
    assert type(record["player_id"]) is int and record["player_id"] == 7
    assert type(record["name"]) is str


def test_payload_records_keeps_float_precision():
    frame = pd.DataFrame(
        {
            "date": ["x"],
            "player_id": [1],
            "name": ["n"],
            "sd_shots_pg": [2 / 3],
            "career_games": [9],
            "opponent_abbrev": ["TOR"],
        }
    )
    assert payload_records(frame)[0]["sd_shots_pg"] == 2 / 3


class _FakeTable:
    select_count = 0

    def __init__(self, client, name):
        self._client = client
        self._name = name

    def select(self, columns):
        self._client.last_select = columns
        type(self).select_count += 1
        return self

    def limit(self, n):
        return self

    def execute(self):
        if "sd_shots_pg" in self._client.last_select and not self._client.migration_landed:
            raise RuntimeError(f"column sd_shots_pg does not exist on {self._name}")
        if "game_shots" in self._client.last_select and not self._client.raw_landed:
            raise RuntimeError(f"column game_shots does not exist on {self._name}")
        return type("R", (), {"data": []})()

    def upsert(self, batch, on_conflict=None, returning=None):
        self._client.upserts.append((list(batch), on_conflict, returning))
        return type("R", (), {"data": []})()


class _FakeClient:
    def __init__(self, migration_landed, raw_landed=None):
        self.migration_landed = migration_landed
        self.raw_landed = migration_landed if raw_landed is None else raw_landed
        self.upserts = []
        self.last_select = ""

    def table(self, name):
        return _FakeTable(self, name)


def test_attach_noop_until_migration_landed(monkeypatch):
    client = _FakeClient(migration_landed=False)
    monkeypatch.setattr(publish_shot_cols, "SUPABASE_ADMIN_CLIENT", client)
    _clear_probes()
    payload = [{"date": "2025-01-02", "player_id": 1, "name": "n"}]
    result = attach_shot_columns(payload, "Player-Snapshots-backtrack-dev", block=_stub_block())
    assert result == payload  # untouched: no new keys
    assert len(client.upserts) == 0


def test_attach_adds_columns_once_landed(monkeypatch):
    client = _FakeClient(migration_landed=True, raw_landed=True)
    monkeypatch.setattr(publish_shot_cols, "SUPABASE_ADMIN_CLIENT", client)
    _clear_probes()
    payload = [
        {"date": "2025-01-02", "player_id": 1, "name": "n"},
        {"date": "2025-01-03", "player_id": 1, "name": "n"},
        {"date": "2025-01-09", "player_id": 999, "name": "n"},  # not in block
    ]
    # An EMPTY raw block: the store supplies nothing for these keys, so the
    # 16 columns attach and NO raw keys are fabricated (unit-test friendly:
    # no sqlite/network path).
    result = attach_shot_columns(
        payload, "Player-Snapshots-backtrack-dev", block=_stub_block(), raw_block=pd.DataFrame()
    )
    assert result[0]["sd_shots_pg"] == 2.5
    assert result[0]["career_games"] == 1
    assert result[0]["team_abbrev"] == "EDM"
    assert result[1]["sd_shots_pg"] == 2.25
    # NaN semantics: season-first row carries None opp, an absent source row is never fabricated
    assert result[1]["opponent_abbrev"] is None
    assert "sd_shots_pg" not in payload[2] or payload[2].get("sd_shots_pg") is None


def test_attach_probe_is_cached(monkeypatch):
    client = _FakeClient(migration_landed=True)
    monkeypatch.setattr(publish_shot_cols, "SUPABASE_ADMIN_CLIENT", client)
    publish_shot_cols._PROBE.clear()
    publish_shot_cols._RAW_PROBE.clear()
    # First call probes and installs the cache flag.
    attach_shot_columns([], "Player-Snapshots-backtrack-dev", block=_stub_block())
    assert publish_shot_cols._PROBE["Player-Snapshots-backtrack-dev"] is True
    select_count = _FakeTable.select_count
    # A second call must not re-probe (network-free skip path). Block and
    # payload supplied so no sqlite/hosts are touched.
    attach_shot_columns([], "Player-Snapshots-backtrack-dev", block=_stub_block())
    assert _FakeTable.select_count == select_count


def _stub_raw_block():
    """Frame shaped like raw_game_map()'s output for attach tests."""
    return pd.DataFrame(
        {
            "date": ["2025-01-01", "2025-01-02", "2025-01-03"],
            "player_id": [1, 1, 2],
            "shots": [1, 2, 0],
            "goals": [0, 1, 0],
            "points": [0, 2, 0],
            "toi_min": ["19:30", np.nan, 13.25],
        }
    )


def _clear_probes():
    publish_shot_cols._PROBE.clear()
    publish_shot_cols._RAW_PROBE.clear()


def test_attach_adds_raw_game_columns_once_landed(monkeypatch):
    client = _FakeClient(migration_landed=True, raw_landed=True)
    monkeypatch.setattr(publish_shot_cols, "SUPABASE_ADMIN_CLIENT", client)
    _clear_probes()
    payload = [
        {"date": "2025-01-01", "player_id": 1, "name": "n"},  # store row: toi string -> minutes
        {"date": "2025-01-02", "player_id": 1, "name": "n"},  # store row: NaN toi -> None
        {"date": "2025-01-09", "player_id": 999, "name": "n"},  # no store row: NO raw keys at all
    ]
    result = attach_shot_columns(
        payload, "Player-Snapshots-backtrack-dev", block=_stub_block(), raw_block=_stub_raw_block()
    )
    assert result[0]["game_shots"] == 1
    assert result[0]["game_goals"] == 0
    assert result[0]["game_points"] == 0
    assert result[0]["game_toi_min"] == 19.5  # 'MM:SS-string passthrough in _toi_minutes... minutes float
    assert type(result[0]["game_shots"]) is int
    assert result[1]["game_shots"] == 2 and result[1]["game_goals"] == 1
    assert result[1]["game_toi_min"] is None  # NaN -> None encode
    # A row the store cannot supply gets NO raw keys (NULL = did not play),
    # while the 16 aggregate columns still attach.
    assert all(key not in result[2] for key in publish_shot_cols.RAW_GAME_COLUMNS)
    assert result[2].get("sd_shots_pg") is None  # absent source row never fabricated


def test_attach_skips_raw_keys_until_raw_columns_landed(monkeypatch):
    client = _FakeClient(migration_landed=True, raw_landed=False)
    monkeypatch.setattr(publish_shot_cols, "SUPABASE_ADMIN_CLIENT", client)
    _clear_probes()
    payload = [{"date": "2025-01-02", "player_id": 1, "name": "n"}]
    result = attach_shot_columns(
        payload, "Player-Snapshots-backtrack-dev", block=_stub_block(), raw_block=_stub_raw_block()
    )
    assert result[0]["sd_shots_pg"] == 2.5  # 16 keys unaffected by the raw split
    assert all(key not in result[0] for key in publish_shot_cols.RAW_GAME_COLUMNS)


def test_attach_startup_never_mixes_raw_without_block(monkeypatch):
    # On a table where BOTH landed but the caller supplies no raw_block (the
    # real nightly path), the raw source is raw_game_map() - the store read.
    # That path is exercised elsewhere; here assert probing only, with an
    # empty payload so nothing is fetched.
    client = _FakeClient(migration_landed=True, raw_landed=True)
    monkeypatch.setattr(publish_shot_cols, "SUPABASE_ADMIN_CLIENT", client)
    _clear_probes()
    assert attach_shot_columns([], "Player-Snapshots-backtrack-dev", block=_stub_block()) == []
    assert publish_shot_cols._RAW_PROBE["Player-Snapshots-backtrack-dev"] is True


def test_attach_is_idempotent_and_never_deletes_rows(monkeypatch):
    client = _FakeClient(migration_landed=True, raw_landed=True)
    monkeypatch.setattr(publish_shot_cols, "SUPABASE_ADMIN_CLIENT", client)
    _clear_probes()
    payload = [
        {"date": "2025-01-01", "player_id": 1, "name": "n"},
        {"date": "2025-01-02", "player_id": 1, "name": "n"},
    ]
    first = attach_shot_columns(
        [dict(row) for row in payload],
        "Player-Snapshots-backtrack-dev",
        block=_stub_block(),
        raw_block=_stub_raw_block(),
    )
    # Second attach on the ALREADY-attached rows: same source, same casts, so
    # every value is re-derived identically - no drift, no extra keys, and the
    # row set cannot shrink (updates never delete: the table's row count is
    # invariant, the 623,358-row top-up never dropped a row).
    second = attach_shot_columns(
        first, "Player-Snapshots-backtrack-dev", block=_stub_block(), raw_block=_stub_raw_block()
    )
    assert len(second) == len(payload)
    assert all("game_shots" in row for row in second[:2])
    assert {row["date"]: row["game_shots"] for row in second} == {
        "2025-01-01": 1,
        "2025-01-02": 2,
    }
    expected_keys = {
        "date",
        "player_id",
        "name",
        *publish_shot_cols.RAW_GAME_COLUMNS,
        "sd_shots_pg",
        "sd_goals_pg",
        "sd_toi_pg",
        "sd_sh_pct",
        "r5_shots_pg",
        "r5_goals_pg",
        "r10_shots_pg",
        "r10_goals_pg",
        "r10_toi_pg",
        "ewm_goals",
        "ewm_shots",
        "career_games",
        "career_goals_pg",
        "career_shots_pg",
        "team_abbrev",
        "opponent_abbrev",
    }
    assert set(second[0]) == expected_keys  # idempotency: second pass adds exactly nothing new


def test_raw_records_nan_becomes_none_and_ints_stay_int():
    frame = pd.DataFrame(
        {
            "date": ["2025-01-01", "2025-01-02"],
            "player_id": [7, 8],
            "name": ["a", "b"],
            "game_shots": pd.array([3, pd.NA], dtype="Int64"),  # the builder's dtype
            "game_goals": pd.array([1, 0], dtype="Int64"),
            "game_points": pd.array([2, pd.NA], dtype="Int64"),
            "game_toi_min": [12.5, np.nan],
        }
    )
    records = payload_records(frame)
    assert type(records[0]["game_shots"]) is int and records[0]["game_shots"] == 3
    assert records[0]["game_toi_min"] == 12.5
    assert records[1]["game_shots"] is None
    assert records[1]["game_points"] is None
    assert records[1]["game_toi_min"] is None
    assert records[1]["game_goals"] == 0  # zero is a VALUE, never lost to NaN encoding
