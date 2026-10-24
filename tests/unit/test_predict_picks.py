"""Unit tests for the predict-only ML picks Lambda (no AWS, no Supabase).

Stubbed surfaces: boto3.client("s3") returns an in-memory fake (the bundle is
joblib-dumped into a BytesIO), the Supabase reads go through a fake table
builder shaped like supabase-py's builder chain, and the Picks write is a
fake save_to_db. Asserted: the 16-column hard fail naming exactly what is
missing, the stale-artifact warning, encode order/categories (the artifact's
order/shape assertion), the ensemble within-group rank rule, one-pick-per-
group assembly, payload whitelisting, and the dry/published handler paths.
"""

import datetime
import importlib.util
import io
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock  # noqa: E402  (bootstrap precedes; see module docstring + stubs)

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[0:0] = [str(ROOT / "smartscore"), str(ROOT / "smartscore" / "scripts" / "backtrack")]

# constants.py expects the compiled prediction wheel (make compile) at import
# time; save_to_db's code path only reads constant strings. Stub the wheel the
# way tests/unit/conftest.py stubs supabase - the integration compile is not a
# dependency of unit-testing the pick assembly.
_rust_stub = types.ModuleType("make_predictions_rust")


class _StubWeights:  # noqa: N801
    def __init__(self, **_kwargs):
        self.__dict__.update(_kwargs)


_rust_stub.Weights = _StubWeights
sys.modules.setdefault("make_predictions_rust", _rust_stub)

# sys.path bootstrap precedes these imports deliberately (see above)
# (bootstrap deliberately precedes these imports)
# noqa anchor
_SPEC = importlib.util.spec_from_file_location(  # noqa: E402

    "ml_picks_handler", ROOT / "smartscore" / "lambda" / "predict_picks" / "handler.py"
)
mod = importlib.util.module_from_spec(_SPEC)
sys.modules["ml_picks_handler"] = mod
_SPEC.loader.exec_module(mod)

model = mod.model


class FakeProba:
    """A 'model' whose positive-class probability column is a chosen array."""

    def __init__(self, values):
        self.values = np.asarray(values, dtype=float)

    def predict_proba(self, x):
        return np.column_stack([1 - self.values[: len(x)], self.values[: len(x)]])


class FakeRanker:
    """Stand-in ranker: predict() scores are higher-is-better."""

    def __init__(self, values):
        self.values = np.asarray(values, dtype=float)

    def predict(self, x):
        return self.values[: len(x)]


def make_bundle(today="2026-06-14"):
    return {
        "hgb": FakeProba([0.1, 0.8, 0.3, 0.6]),
        "lgbm": FakeProba([0.2, 0.9, 0.4, 0.1]),
        "logreg": FakeProba([0.3, 0.7, 0.5, 0.0]),
        "ranker": FakeRanker([0.5, 0.4, 0.9, 0.2]),
        "numeric_features": ["gpg", "hgpg"],
        "categorical_features": ["tims"],
        "all_features": ["gpg", "hgpg", "tims"],
        "categories": {"tims": ["1", "2", "3"]},
        "metadata": {"train_through": today, "seed": 0, "git_hash": "abc1234"},
    }


@pytest.fixture
def env_vars(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("ML_S3_BUCKET", "test-artifacts")
    monkeypatch.delenv("ML_ARTIFACT_PREFIX", raising=False)
    monkeypatch.delenv("ML_HISTORY_WINDOW_DAYS", raising=False)


# ------------------------------------------------------------------ fakes


class FakeResult:
    """\\.range() result: exposes .data as row dicts."""

    def __init__(self, frame):
        self.data = frame.to_dict(orient="records")


class FakeTable:
    def __init__(self, frame):
        self._frame = frame
        self._low = None
        self._high = None

    def select(self, *_cols, **_kw):
        return self

    def gte(self, _col, value):
        self._low = value
        return self

    def lt(self, _col, value):
        self._high = value
        return self

    def order(self, _col, **_kw):
        return self

    def range(self, lo, hi):
        rows = self._frame
        if self._low is not None:
            rows = rows[rows["date"] >= self._low]
        if self._high is not None:
            rows = rows[rows["date"] < self._high]
        self._slice = rows.iloc[lo : min(hi + 1, len(rows))]
        return self

    def execute(self):
        return FakeResult(self._slice)


ON_DATE = "2026-06-14"
HISTORY_DATE = "2026-06-10"


def fake_players():
    prior = pd.DataFrame({
        "date": [HISTORY_DATE] * 4,
        "player_id": [1, 2, 9, 10],
        "name": ["a", "b", "g", "h"],
        "team_name": ["T", "T", "Time", "Noise"],
        "home": [1, 1, 0, 0],
        "tims": [0, 0, 0, 0],
        "scored": [0, 1, 0, 0],
        "gpg": [0.5, 0.2, 0.0, 0.0],
        "hgpg": [0.1, 0.2, 0.0, 0.0],
        "five_gpg": [0.1, 0.2, 0.0, 0.0],
        "ppg": [0.2, 0.1, 0.0, 0.0],
        "tgpg": [3.0, 3.0, 0.0, 0.0],
        "otga": [2.5, 2.5, 0.0, 0.0],
        "otshga": [0.01, 0.01, 0.0, 0.0],
        "injury_status": [None] * 4,
        "lineup_unit": [None] * 4,
        "lineup_position_group": ["C", "W", None, None],
        "pp_unit": [None] * 4,
        "lineup_status": [None] * 4,
        "team_abbrev": ["TOR", "TOR", "MTL", "MTL"],
        "opponent_abbrev": ["MTL", "MTL", "TOR", "TOR"],
        **{c: [0.0] * 4 for c in mod.REQUIRED_SNAPSHOT_COLUMNS if c not in ("team_abbrev", "opponent_abbrev")},
    })
    today = prior.copy()
    today["date"] = ON_DATE
    today["scored"] = None
    today["tims"] = [1, 1, 2, 3]
    today["lineup_position_group"] = ["C", "W", "C", "W"]
    return pd.concat([prior, today], ignore_index=True)


def fake_teams():
    return pd.DataFrame({
        "date": [HISTORY_DATE, ON_DATE] * 2,
        "team_abbrev": ["TOR", "TOR", "MTL", "MTL"],
        "tgpg": [3.0] * 4,
        "otga": [2.5] * 4,
        "otshga": [0.01] * 4,
        "season": ["20252026"] * 4,
    })


def fake_goalies():
    return pd.DataFrame({
        "date": [HISTORY_DATE, ON_DATE] * 2,
        "player_id": [9, 9, 10, 10],
        "team_abbrev": ["MTL", "MTL", "TOR", "TOR"],
        "gaa": [2.5] * 4,
        "save_pct": [0.9] * 4,
        "record": ["10-4-2"] * 4,
        "shutouts": [1] * 4,
        "games_played": [10, 10, 12, 12],
    })


@pytest.fixture
def prediction_tables(monkeypatch):
    tables = {"Player-Snapshots": fake_players(), "Team-Stats": fake_teams(), "Default": fake_goalies()}

    fake_client = MagicMock(name="supabase_admin")

    def _table(name):
        for prefix, frame in tables.items():
            if name.startswith(prefix):
                return FakeTable(frame)
        return FakeTable(tables["Default"])

    fake_client.table.side_effect = _table
    monkeypatch.setattr("ml_picks_handler.SUPABASE_ADMIN_CLIENT", fake_client)
    return fake_client


def fake_s3(monkeypatch, bundle):
    import joblib  # noqa: PLC0415

    buffer = io.BytesIO()
    joblib.dump(bundle, buffer, compress=("zlib", 3))
    payload = buffer.getvalue()

    s3 = MagicMock()
    s3.list_objects_v2.return_value = {
        "Contents": [
            {"Key": "predict/ml/artifact_2026-06-13_0.joblib"},
            {"Key": "predict/ml/artifact_2026-06-14_0.joblib"},
            {"Key": "predict/ml/nothing.zip"},
        ]
    }
    s3.get_object.return_value = {"Body": io.BytesIO(payload)}
    monkeypatch.setattr("ml_picks_handler.boto3.client", lambda service, **_kw: s3)
    return s3


def fake_save(monkeypatch, calls):
    def _fake(rows):
        calls.extend(rows)
        return rows

    monkeypatch.setattr("ml_picks_handler.save_to_db", _fake)


# --------------------------------------------------------------- artifacts


def test_load_artifact_picks_lexically_latest_joblib(env_vars, monkeypatch):
    s3 = fake_s3(monkeypatch, make_bundle())
    key, loaded = mod.load_artifact(s3, "test-artifacts", "predict/ml")
    assert key.endswith("artifact_2026-06-14_0.joblib")
    assert loaded["metadata"]["seed"] == 0


def test_load_artifact_empty_prefix_raises(env_vars, monkeypatch):
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {"Contents": []}
    with pytest.raises(RuntimeError, match="no artifact"):
        mod.load_artifact(s3, "test-artifacts", "predict/ml")


def test_staleness_note_warns_beyond_7_days(env_vars, capsys):
    stale = make_bundle()
    stale["metadata"]["train_through"] = (datetime.date.today() - datetime.timedelta(days=8)).isoformat()
    assert mod.staleness_note(stale) == 8
    assert "8 days stale" in capsys.readouterr().out

    fresh = make_bundle()
    fresh["metadata"]["train_through"] = datetime.date.today().isoformat()
    assert mod.staleness_note(fresh) == 0
    assert "stale" not in capsys.readouterr().out


def test_require_snapshot_columns_hard_fail_names_exact_missing():
    rows = pd.DataFrame([{c: 1.0 for c in ("gpg", "sd_shots_pg", "team_abbrev")}])
    with pytest.raises(RuntimeError) as err:
        mod.require_snapshot_columns(rows)
    msg = str(err.value)
    for col in ("sd_goals_pg", "career_games", "opponent_abbrev"):
        assert col in msg
    assert "missing exactly" in msg


# ------------------------------------------------------------------ encode


def test_encode_frame_matches_artifact_column_order():
    frame = pd.DataFrame({"gpg": [0.5, 0.2, 0.9], "hgpg": [0.1, 0.2, 0.3], "tims": [1, 2, 3]})
    x = mod.encode_frame(make_bundle(), frame)
    assert list(x.columns) == ["gpg", "hgpg", "tims"]  # the artifact's order, not the frame's
    assert list(x["tims"].cat.categories) == ["1", "2", "3"]
    assert x["gpg"].notna().all()


def test_encode_frame_unseen_category_maps_to_nan():
    frame = pd.DataFrame({"gpg": [0.5], "hgpg": [0.1], "tims": [7]})
    x = mod.encode_frame(make_bundle(), frame)
    assert pd.isna(x["tims"].iloc[0])  # unseen -> NaN (training-side policy)


def test_ensemble_scores_rank_rule():
    frame = pd.DataFrame({
        "date": [ON_DATE] * 3,
        "player_id": [1, 2, 3],
        "tims": [1, 1, 2],
        "gpg": [0.5, 0.2, 0.9],
        "hgpg": [0.1, 0.2, 0.3],
    })
    sim = mod.ensemble_scores(make_bundle(), frame)
    assert sim["ensemble"].between(0.0, 1.0).all()
    group1 = sim[sim["tims"] == 1]
    # the real model outputs (index 1) dominate both other members inside
    # tims=1: hgb 0.8 vs 0.1, lgbm 0.9 vs 0.2, logreg 0.7 vs 0.3
    assert group1["ensemble"].idxmax() == 1


def test_supabase_frame_matches_artifact_features(env_vars, prediction_tables):
    players, teams, goalies = mod.load_prediction_tables("prod", ON_DATE)
    mod.require_snapshot_columns(players[players["date"].astype(str) == ON_DATE])
    frame = model.build_frame(players, teams, goalies, source="supabase")
    frame = model.add_derived(frame, teams, source="supabase")
    frame = frame[frame["date"].astype(str) == ON_DATE].reset_index(drop=True)
    bundle = make_bundle()
    assert not frame.empty
    assert all(col in frame.columns for col in bundle["all_features"])
    x = mod.encode_frame(bundle, frame)
    assert list(x.columns) == bundle["all_features"]  # same order/shape as the artifact


# ----------------------------------------------------------- pick assembly


def test_pick_rows_one_per_group_and_order():
    frame = pd.DataFrame({
        "date": [ON_DATE, ON_DATE, ON_DATE, ON_DATE],
        "player_id": [1, 2, 9, 10],
        "name": ["a", "b", "g", "h"],
        "team_name": ["T", "T", "Time", "Noise"],
        "tims": [1, 1, 2, 3],
        "ensemble": [0.4, 0.8, 0.6, np.nan],
    })
    picks = model.daily_picks(frame, "ensemble")
    row_map = dict(zip(picks["tims"], picks["player_id"]))
    # highest ensemble member per group; tims=3 is all-NaN -> daily_picks
    # (documented) skips that group, and a real slate would then hard-fail in
    # validate_picks instead of publishing an ambiguous slate
    assert row_map == {1: 2, 2: 9}


def test_validate_picks_rejects_wrong_shape():
    sim = pd.DataFrame({
        "date": [ON_DATE] * 2,
        "player_id": [1, 2],
        "tims": [1, 1],
        "ensemble": [0.5, 0.6],
    })
    picks = model.daily_picks(sim, "ensemble")
    with pytest.raises(RuntimeError, match="3 picks"):
        mod.validate_picks(picks, ON_DATE)


def test_pick_payload_whitelist():
    picks = pd.DataFrame({
        "date": [ON_DATE],
        "player_id": [1.0],
        "name": ["a"],
        "team_name": ["T"],
        "tims": [1.0],
        "gpg": [0.5],
        "hgpg": [0.1],
        "ppg": [0.2],
        "ensemble": [0.12345],
        "scored": [np.nan],
    })
    rows = mod.pick_rows_to_picks_db(picks)

    assert len(rows) == 1
    assert rows[0] == {
        "date": ON_DATE,
        "player_id": 1,
        "name": "a",
        "team_name": "T",
        "stat": 12.3,        # ensemble ranked-pct -> percent, 1 decimal
        "tims": 1,
        "gpg": 0.5,
        "hgpg": 0.1,
        "ppg": 0.2,
    }


# ------------------------------------------------------ handler end-to-end


def test_handler_publishes_three_picks(env_vars, prediction_tables, monkeypatch):
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls = []
    fake_save(monkeypatch, calls)
    result = mod.handler({"date": ON_DATE}, None)
    assert result["statusCode"] == 200 and not result["dry"]
    assert len(calls) == 3 and sorted(r["tims"] for r in calls) == [1, 2, 3]
    assert all(r["date"] == ON_DATE and r["stat"] is not None for r in calls)


def test_handler_dry_skips_write(env_vars, prediction_tables, monkeypatch):
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls = []
    fake_save(monkeypatch, calls)
    result = mod.handler({"date": ON_DATE, "dry": True}, None)
    assert result["dry"] and calls == []


def test_handler_missing_columns_fails_instead_of_publishing(env_vars, prediction_tables, monkeypatch):
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    players = pd.DataFrame([{"date": ON_DATE, "player_id": 1, "name": "a"}])
    monkeypatch.setattr("ml_picks_handler.load_prediction_tables", lambda _env, _d: (players, players, players))
    calls = []
    fake_save(monkeypatch, calls)
    with pytest.raises(RuntimeError, match="sd_goals_pg"):
        mod.handler({"date": ON_DATE}, None)
    assert calls == []


def test_handler_ambiguous_slate_fails(env_vars, prediction_tables, monkeypatch):
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls = []
    fake_save(monkeypatch, calls)

    def _only_one_group(sim, col):
        return sim[sim["tims"] == 1].head(1)

    monkeypatch.setattr("ml_picks_handler.model.daily_picks", _only_one_group)
    with pytest.raises(RuntimeError, match="3 picks"):
        mod.handler({"date": ON_DATE}, None)
    assert calls == []
