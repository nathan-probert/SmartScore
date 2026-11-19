"""Unit tests for the Storage ledger + the daily run's features path.

Covers:
* ml_ledger: season split (COVID exceptions), materialize, deterministic
  gzip round-trip sizes (the change detector push relies on);
* run_daily_picks.features_rows: shape guard (the transition probe);
* run_daily_picks._merge_features / load_prediction_tables' features path;
* publish_daily_features.compute_features: the exact frame pipeline over a
  tiny synthetic ledger, with the NHL schedule stubbed.
"""

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "smartscore"))
sys.path.insert(1, str(ROOT / "smartscore" / "scripts" / "backtrack"))

patcher = patch("supabase.create_client", return_value=MagicMock())
patcher.start()


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "smartscore" / "scripts" / "backtrack" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mls = _load("ml_history_store")
ml = _load("ml_ledger")
pdf = _load("publish_daily_features")
rdp = _load("run_daily_picks")
model = _load("model")

PLAYER_TABLE = "Player-Snapshots-backtrack-prod"
TEAM_TABLE = "Team-Stats-backtrack-prod"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ML_HISTORY_BACKEND", "sqlite")
    monkeypatch.setenv("ML_HISTORY_DB", str(tmp_path / "ml_history.sqlite"))
    monkeypatch.delenv("ML_HISTORY_DUAL_WRITE", raising=False)
    return mls


# ------------------------------------------------------------- ledger split


@pytest.mark.usefixtures("store")
def test_split_materialize_roundtrip(tmp_path):
    mls.upsert_rows(
        PLAYER_TABLE,
        [
            {"date": "2024-11-01", "player_id": 1, "name": "A", "game_shots": 2},
            {"date": "2025-11-01", "player_id": 1, "name": "A", "game_shots": 3},
            {"date": "2020-08-15", "player_id": 2, "name": "B", "game_shots": 1},  # COVID bubble
        ],
    )
    mls.upsert_rows(
        TEAM_TABLE,
        [
            {"season": "20242025", "team_abbrev": "TOR", "game_id": 9, "date": "2024-11-01", "tgpg": 3.0},
            {"season": "20252026", "team_abbrev": "TOR", "game_id": 10, "date": "2025-11-01", "tgpg": 3.1},
        ],
    )
    parts = ml.split_seasons(os.environ["ML_HISTORY_DB"], tmp_path / "split")
    assert sorted(parts) == ["20192020", "20242025", "20252026"]
    merged = ml.materialize(parts, tmp_path / "working.sqlite")
    con = sqlite3.connect(merged)
    counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in mls.TABLE_MAP.values()}
    con.close()
    assert counts["player_snapshots_backtrack"] == 3
    assert counts["team_stats_backtrack"] == 2


@pytest.mark.usefixtures("store")
def test_gzip_deterministic_same_bytes(tmp_path):
    """push()'s change detector: an unchanged season re-compresses to the
    identical byte count."""
    mls.upsert_rows(PLAYER_TABLE, [{"date": "2024-11-01", "player_id": 1, "name": "A"}])
    parts = ml.split_seasons(os.environ["ML_HISTORY_DB"], tmp_path / "a")
    size_a = ml._gzipped(parts["20242025"]).stat().st_size
    size_b = ml._gzipped(parts["20242025"]).stat().st_size
    assert size_a == size_b


def test_season_of_covid_exceptions():
    assert ml.season_of("2020-08-15") == "20192020"
    assert ml.season_of("2021-07-15") == "20202021"
    assert ml.season_of("2026-11-18") == "20262027"
    assert ml.season_of("2026-06-15") == "20252026"


# ------------------------------------------------------- features probe/merge


@pytest.mark.usefixtures("store")
def test_features_rows_shape_guard(monkeypatch):
    fake_client = MagicMock(name="supabase_admin")
    # a stub answering with a non-features shape (the pre-migration /
    # default-fake case) must fall back, not crash
    fake_client.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"id": 1, "date": "2026-11-19"}
    ]
    monkeypatch.setattr(rdp, "SUPABASE_ADMIN_CLIENT", fake_client)
    assert rdp.features_rows("2026-11-19") is None


@pytest.mark.usefixtures("store")
def test_merge_features_resolves_club(monkeypatch):
    today = pd.DataFrame(
        [
            {"date": "2026-11-19", "player_id": 1, "name": "A", "home": True, "tims": 1, "scored": None},
        ]
    )
    features = pd.DataFrame(
        [
            {
                "date": "2026-11-19",
                "player_id": 1,
                "name": "A",
                "club": "TOR",
                "opp": "BOS",
                **{col: 0.5 for col in model.SHOT_FEATURES},
                **{col: 1.0 for col in pdf.STREAK_COLUMNS},
            }
        ]
    )
    merged = rdp._merge_features(today, features, "2026-11-19")
    assert merged.iloc[0]["team_abbrev"] == "TOR"
    assert merged.iloc[0]["opponent_abbrev"] == "BOS"
    assert merged.iloc[0]["sd_shots_pg"] == 0.5
    assert merged.iloc[0]["club_rest_days"] == 1.0


# ------------------------------------------------- the nightly feature compute


@pytest.mark.usefixtures("store")
def test_compute_features_over_synthetic_ledger(monkeypatch):
    """The full publisher pipeline over a tiny ledger with a stubbed NHL
    schedule: the published row must carry the EXACT values the training
    core computes for the synthetic pre-game row."""
    D = "2026-11-19"
    # ledger: player 1 scored 1 goal on 3 shots in ONE prior game on 11-17,
    # for TOR. add_derived over [prior row, synthetic D row] must yield
    # sd_goals_pg=1/1, sd_shots_pg=3/1, prev2_goals=1 (the prior label),
    # days_since_game=2.
    mls.upsert_rows(
        PLAYER_TABLE,
        [
            {
                "date": "2026-11-17",
                "player_id": 7,
                "name": "Test Player",
                "team_name": "Toronto",
                "home": True,
                "scored": 1,
                "gpg": 1.0,
                "game_shots": 3,
                "game_goals": 1,
                "game_toi_min": 15.0,
                "team_abbrev": "TOR",
                "opponent_abbrev": "MTL",
            }
        ],
    )
    mls.upsert_rows(
        TEAM_TABLE,
        [
            {"season": "20262027", "team_abbrev": "TOR", "game_id": 1, "date": "2026-11-17", "tgpg": 3.0},
        ],
    )
    mls.upsert_rows(
        "Goalie-Snapshots-backtrack-prod",
        [
            {
                "season": "20262027",
                "player_id": 99,
                "team_abbrev": "MTL",
                "team_name": "Montreal",
                "date": "2026-11-17",
                "name": "Test Goalie",
                "gaa": 2.9,
                "save_pct": 0.9,
                "record": "5-2-0",
                "shutouts": 1,
                "games_played": 10,
            }
        ],
    )
    monkeypatch.setattr(rdp, "SUPABASE_ADMIN_CLIENT", MagicMock(name="supabase_admin"))
    schedule = {
        "gameWeek": [
            {
                "date": D,
                "games": [
                    {
                        "gameType": 2,
                        "homeTeam": {"abbrev": "TOR"},
                        "awayTeam": {"abbrev": "MTL"},
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(pdf.nhl_client, "fetch_schedule", lambda date: schedule)

    published = pdf.compute_features(D, "prod", dry=True)
    assert published is not None and len(published) == 1
    row = published.iloc[0]
    assert row["player_id"] == 7
    assert row["club"] == "TOR"
    assert row["opp"] == "MTL"
    assert row["sd_goals_pg"] == pytest.approx(1.0)
    assert row["sd_shots_pg"] == pytest.approx(3.0)
    assert row["career_games"] == 1
    assert row["prev2_goals"] == 1.0
    assert row["days_since_game"] == 2


@pytest.mark.usefixtures("store")
def test_compute_features_no_slate(monkeypatch):
    monkeypatch.setattr(rdp, "SUPABASE_ADMIN_CLIENT", MagicMock(name="supabase_admin"))
    monkeypatch.setattr(pdf.nhl_client, "fetch_schedule", lambda date: {"gameWeek": []})
    assert pdf.compute_features("2026-11-19", "prod", dry=True) is None


# ------------------------------------------------------------ run features path


def test_load_prediction_tables_features_path(monkeypatch):
    """features_rows returning rows switches load_prediction_tables to the
    features path: history=None, club/opp resolved from the feature rows,
    NO big-table reads."""
    live = pd.DataFrame(
        [
            {
                "date": "2026-11-19",
                "player_id": 1,
                "name": "A",
                "team_name": "Toronto",
                "home": True,
                "tims": 1,
                "scored": None,
                "gpg": 0.5,
                "hgpg": 0.4,
                "five_gpg": 0.6,
                "ppg": 0.1,
                "hppg": 0.2,
                "tgpg": 3.0,
                "otga": 2.5,
                "otshga": 0.5,
                "injury_status": "HEALTHY",
                "injury_desc": "",
                "opp_goalie_name": "G",
                "opp_goalie_team": "MTL",
                "opp_goalie_gaa": 2.9,
                "opp_goalie_save_pct": 0.9,
                "opp_goalie_record": "5-2-0",
                "opp_goalie_shutouts": 1,
                "opp_goalie_games_played": 10,
                "lineup_unit": "F1",
                "lineup_position_group": "F",
                "pp_unit": "POWER PLAY #1",
                "lineup_status": "PROJECTED",
            }
        ]
    )
    features = pd.DataFrame(
        [
            {
                "date": "2026-11-19",
                "player_id": 1,
                "name": "A",
                "club": "TOR",
                "opp": "MTL",
                **{col: 1.0 for col in model.SHOT_FEATURES},
                **{col: 2.0 for col in pdf.STREAK_COLUMNS},
            }
        ]
    )
    monkeypatch.setattr(rdp, "features_rows", lambda on_date: features)
    monkeypatch.setattr(rdp, "today_live_rows", lambda env, date: live.copy())
    loaded = rdp.load_prediction_tables("prod", "2026-11-19")
    today, players, teams, goalies, history = loaded
    assert history is None
    assert today.iloc[0]["team_abbrev"] == "TOR"
    assert today.iloc[0]["sd_shots_pg"] == 1.0
    # the injected context rows exist for build_frame's merges
    assert not teams.empty and not goalies.empty
