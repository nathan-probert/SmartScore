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
        return type("R", (), {"data": []})()

    def upsert(self, batch, on_conflict=None, returning=None):
        self._client.upserts.append((list(batch), on_conflict, returning))
        return type("R", (), {"data": []})()


class _FakeClient:
    def __init__(self, migration_landed):
        self.migration_landed = migration_landed
        self.upserts = []
        self.last_select = ""

    def table(self, name):
        return _FakeTable(self, name)


def test_attach_noop_until_migration_landed(monkeypatch):
    client = _FakeClient(migration_landed=False)
    monkeypatch.setattr(publish_shot_cols, "SUPABASE_ADMIN_CLIENT", client)
    payload = [{"date": "2025-01-02", "player_id": 1, "name": "n"}]
    result = attach_shot_columns(payload, "Player-Snapshots-backtrack-dev", block=_stub_block())
    assert result == payload  # untouched: no new keys
    assert len(client.upserts) == 0


def test_attach_adds_columns_once_landed(monkeypatch):
    client = _FakeClient(migration_landed=True)
    monkeypatch.setattr(publish_shot_cols, "SUPABASE_ADMIN_CLIENT", client)
    publish_shot_cols._PROBE.clear()
    payload = [
        {"date": "2025-01-02", "player_id": 1, "name": "n"},
        {"date": "2025-01-03", "player_id": 1, "name": "n"},
        {"date": "2025-01-09", "player_id": 999, "name": "n"},  # not in block
    ]
    result = attach_shot_columns(payload, "Player-Snapshots-backtrack-dev", block=_stub_block())
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
    # First call probes and installs the cache flag.
    attach_shot_columns([], "Player-Snapshots-backtrack-dev", block=_stub_block())
    assert publish_shot_cols._PROBE["Player-Snapshots-backtrack-dev"] is True
    select_count = _FakeTable.select_count
    # A second call must not re-probe (network-free skip path). Block and
    # payload supplied so no sqlite/hosts are touched.
    attach_shot_columns([], "Player-Snapshots-backtrack-dev", block=_stub_block())
    assert _FakeTable.select_count == select_count
