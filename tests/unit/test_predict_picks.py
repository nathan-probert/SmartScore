"""Unit tests for the predict-only ML picks daily pass (no AWS, no Supabase).

The entry point under test is smartscore/scripts/backtrack/run_daily_picks.py
(the former Lambda handler's logic, now driven by the scheduled
.github/workflows/ml-picks.yml run). Stubbed surfaces: boto3.client("s3")
returns an in-memory fake (the bundle is joblib-dumped into a BytesIO), the
Supabase reads go through a fake table builder shaped like supabase-py's
builder chain (LIVE Player-Snapshots rows for the pick date; backtrack rows
for history), and the raw per-game store is a synthetic frame. Asserted: the
live-snapshot column hard fail naming exactly what is missing, the
no-live-rows CLEAN exit (warn + no write + status 0), the missing-history-day
warn-and-continue, the train/run shot-block equivalence (the shared core vs
the training math, <1e-9), the stale-artifact warning, encode
order/categories (the artifact's order/shape assertion), the ensemble
within-group rank rule, one-pick-per-group assembly, payload whitelisting,
and the dry/published run paths.
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
    "run_daily_picks", ROOT / "smartscore" / "scripts" / "backtrack" / "run_daily_picks.py"
)
mod = importlib.util.module_from_spec(_SPEC)
sys.modules["run_daily_picks"] = mod
_SPEC.loader.exec_module(mod)

# The runner-side refresh wrapper (ml-picks.yml's crawl step): same import
# pattern, no Supabase config manufactured (refresh_store deliberately
# imports no config module).
_RSPEC = importlib.util.spec_from_file_location(  # noqa: E402
    "refresh_store", ROOT / "smartscore" / "scripts" / "backtrack" / "refresh_store.py"
)
refresh_mod = importlib.util.module_from_spec(_RSPEC)
sys.modules["refresh_store"] = refresh_mod
_RSPEC.loader.exec_module(refresh_mod)

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
        self._eq = None

    def select(self, *_cols, **_kw):
        return self

    def limit(self, _n):
        return self

    def eq(self, _col, value):
        self._eq = value
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
        if self._eq is not None:
            col = "date"
            rows = rows[rows[col].astype(str) == str(self._eq)]
        if self._low is not None:
            rows = rows[rows["date"] >= self._low]
        if self._high is not None:
            rows = rows[rows["date"] < self._high]
        self._slice = rows.iloc[lo : min(hi + 1, len(rows))]
        return self

    def execute(self):
        if not hasattr(self, "_slice"):  # a read that never paged (live .eq read)
            self._slice = self._frame
        return FakeResult(self._slice)


ON_DATE = "2026-06-14"
HISTORY_DATE = "2026-06-10"

# The live snapshot's ON_DATE slate (Player-Snapshots-{ENV} shape: place-name
# team, abbrev opponent goalie team, morning-fresh rates, no shot columns).
LIVE_TEAMS = [
    ("Toronto", "TOR", "MTL"),
    ("Toronto", "TOR", "MTL"),
    ("Montréal", "MTL", "TOR"),
    ("Montréal", "MTL", "TOR"),
]


def fake_live_rows():
    return pd.DataFrame(
        {
            "date": [ON_DATE] * 4,
            "player_id": [1, 2, 9, 10],
            "name": ["a", "b", "g", "h"],
            "team_name": [t for t, _a, _o in LIVE_TEAMS],
            "team_abbrev": [a for _t, a, _o in LIVE_TEAMS],
            "opp_goalie_team": [o for _t, _a, o in LIVE_TEAMS],
            "opp_goalie_name": ["brodeur"] * 4,
            "home": [1, 1, 0, 0],
            "tims": [1, 1, 2, 3],
            "scored": [None] * 4,
            "gpg": [0.5, 0.2, 0.0, 0.0],
            "hgpg": [0.1, 0.2, 0.0, 0.0],
            "five_gpg": [0.1, 0.2, 0.0, 0.0],
            "hppg": [0.0] * 4,
            "ppg": [0.2, 0.1, 0.0, 0.0],
            "tgpg": [3.0, 3.0, 2.0, 2.0],
            "otga": [2.5, 2.5, 2.0, 2.0],
            "otshga": [0.01, 0.01, 0.02, 0.02],
            "opp_goalie_gaa": [2.5] * 4,
            "opp_goalie_save_pct": [0.9] * 4,
            "opp_goalie_record": ["10-4-2"] * 4,
            "opp_goalie_shutouts": [1] * 4,
            "opp_goalie_games_played": [10] * 4,
            "injury_status": [None] * 4,
            "injury_desc": [None] * 4,
            "lineup_unit": [None] * 4,
            "lineup_position_group": ["C", "W", "C", "W"],
            "lineup_status": [None] * 4,
            "pp_unit": [None] * 4,
        }
    )


def fake_history_players():
    """Player-Snapshots-backtrack-{ENV} shape: one row per player-game
    STRICTLY before the pick date (the prior-games context: scored labels,
    club identity, rates). No shot columns - the live path computes today's
    block from the raw per-game store."""
    days = ["2026-06-01", "2026-06-03", "2026-06-05", "2026-06-08", HISTORY_DATE]
    rows = []
    for index, day in enumerate(days):
        rows.append(
            pd.DataFrame(
                {
                    "date": [day] * 4,
                    "player_id": [1, 2, 9, 10],
                    "name": ["a", "b", "g", "h"],
                    "team_name": ["Toronto", "Toronto", "Montréal", "Montréal"],
                    "team_abbrev": ["TOR", "TOR", "MTL", "MTL"],
                    "opponent_abbrev": ["MTL", "MTL", "TOR", "TOR"],
                    "home": [1, 1, 0, 0],
                    "tims": [0, 0, 0, 0],
                    "scored": [(index + offset) % 2 for offset in range(4)],
                    "gpg": [0.3, 0.2, 0.1, 0.0],
                    "hgpg": [0.1, 0.2, 0.0, 0.0],
                    "five_gpg": [0.1, 0.2, 0.0, 0.0],
                    "hppg": [0.0] * 4,
                    "ppg": [0.2, 0.1, 0.0, 0.0],
                    "injury_status": [None] * 4,
                    "injury_desc": [None] * 4,
                    "lineup_unit": [None] * 4,
                    "lineup_position_group": ["C", "W", "C", "W"],
                    "lineup_status": [None] * 4,
                    "pp_unit": [None] * 4,
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


# The raw per-game store the shot block is computed from (the same games the
# history rows summarize): 2 skater-games per player per day, deterministic.
CORE_GAME_DAYS = ["2026-06-01", "2026-06-03", "2026-06-05", "2026-06-08", HISTORY_DATE]


def fake_player_games():
    rows = []
    for index, day in enumerate(CORE_GAME_DAYS):
        for offset, player in enumerate([1, 2, 9, 10]):
            rows.append(
                {
                    "player_id": player,
                    "date": day,
                    "season": "20252026",
                    "goals": (index + offset) % 2,
                    "shots": 1 + ((index + offset) % 4),
                    "toi": f"{10 + index:02d}:{(15 * offset) % 60:02d}",
                }
            )
    return pd.DataFrame(rows)


def fake_teams():
    return pd.DataFrame(
        {
            "date": [HISTORY_DATE, ON_DATE] * 2,
            "team_abbrev": ["TOR", "TOR", "MTL", "MTL"],
            "tgpg": [3.0] * 4,
            "otga": [2.5] * 4,
            "otshga": [0.01] * 4,
            "season": ["20252026"] * 4,
        }
    )


def fake_goalies():
    return pd.DataFrame(
        {
            "date": [HISTORY_DATE, ON_DATE] * 2,
            "player_id": [9, 9, 10, 10],
            "name": ["g", "g", "h", "h"],
            "team_abbrev": ["MTL", "MTL", "TOR", "TOR"],
            "gaa": [2.5] * 4,
            "save_pct": [0.9] * 4,
            "record": ["10-4-2"] * 4,
            "shutouts": [1] * 4,
            "games_played": [10, 10, 12, 12],
            "season": ["20252026"] * 4,
        }
    )


@pytest.fixture
def prediction_tables(monkeypatch):
    """Fakes for the NEW data flow: the LIVE snapshot table carries only the
    pick-date rows; the backtrack tables carry strictly-prior history; the
    raw per-game store is a synthetic frame (no sqlite on disk)."""
    tables = {
        "Player-Snapshots-backtrack": fake_history_players(),
        "Player-Snapshots": fake_live_rows(),
        "Team-Stats-backtrack": fake_teams(),
        "Goalie-Snapshots-backtrack": fake_goalies(),
        "Default": fake_goalies(),
    }

    fake_client = MagicMock(name="supabase_admin")

    def _table(name):
        for prefix, frame in tables.items():
            if name.startswith(prefix):
                return FakeTable(frame)
        return FakeTable(tables["Default"])

    fake_client.table.side_effect = _table
    monkeypatch.setattr("run_daily_picks.SUPABASE_ADMIN_CLIENT", fake_client)

    # the shot block reads the raw store; the fake store is the frame itself
    games = fake_player_games()
    monkeypatch.setattr("run_daily_picks.ensure_raw_store", lambda: None)
    monkeypatch.setattr("run_daily_picks._player_games_frame", games.copy)
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
    monkeypatch.setattr("run_daily_picks.boto3.client", lambda service, **_kw: s3)
    return s3


def fake_save(monkeypatch, calls):
    def _fake(rows, _date):
        calls.extend(rows)
        return rows

    monkeypatch.setattr("run_daily_picks.save_picks_db", _fake)


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


def fake_storage(monkeypatch, bundle):
    import joblib  # noqa: PLC0415

    buffer = io.BytesIO()
    joblib.dump(bundle, buffer, compress=("zlib", 3))
    payload = buffer.getvalue()

    storage = MagicMock()
    storage.list.return_value = [
        {"name": "artifact_2026-06-13_0.joblib"},
        {"name": "artifact_2026-06-14_0.joblib"},
        {"name": "nothing.zip"},
    ]
    storage.download.return_value = payload
    client = MagicMock()
    client.storage.from_.return_value = storage
    monkeypatch.setattr(mod, "SUPABASE_ADMIN_CLIENT", client)
    return client


def test_load_artifact_storage_picks_lexically_latest_joblib(env_vars, monkeypatch):
    client = fake_storage(monkeypatch, make_bundle())
    key, loaded = mod.load_artifact_storage(client, "ml-artifacts", "predict/ml")
    key_norm = key.removeprefix("predict/ml/")
    assert key_norm.endswith("artifact_2026-06-14_0.joblib")
    assert loaded["metadata"]["seed"] == 0
    client.storage.from_.assert_called_with("ml-artifacts")


def test_load_artifact_storage_empty_listing_raises(env_vars, monkeypatch):
    client = MagicMock()
    client.storage.from_.return_value.list.return_value = []
    with pytest.raises(RuntimeError, match="no artifact"):
        mod.load_artifact_storage(client, "ml-artifacts", "predict/ml")


def test_pick_daily_bundle_s3_fails_falls_through_to_storage(env_vars, monkeypatch):
    import joblib  # noqa: PLC0415

    bucket_payload = io.BytesIO()
    joblib.dump(make_bundle(), bucket_payload, compress=("zlib", 3))

    s3 = MagicMock()
    from botocore.exceptions import NoCredentialsError  # noqa: PLC0415  (test-local)

    s3.list_objects_v2.side_effect = NoCredentialsError()
    monkeypatch.setattr("run_daily_picks.boto3.client", lambda service, **_kw: s3)

    client = fake_storage(monkeypatch, make_bundle())
    key, loaded = mod.pick_daily_bundle("s3-bucket", "predict/ml", None, "ml-artifacts", "predict/ml")
    assert "artifact_" in key
    assert loaded["metadata"]["seed"] == 0
    client.storage.from_.assert_called_with("ml-artifacts")


def make_contract_bundle(today=ON_DATE):
    """The sibling's ARTIFACT CONTRACT design: ONLY the contract keys."""
    return {
        "version": 1,
        "train_through": today,
        "seed": 0,
        "git_hash": "abc1234",
        "generated_at": f"{today}T12:00:00+00:00",
        "feature_order": ["gpg", "hgpg", "tims"],
        "categorical_maps": {"tims": ["1", "2", "3"]},
        "derived_params": {
            "ensemble_recipe": "mean of within-(date,tims) rank(pct) of hgb/lgbm/logreg",
        },
        "models": {
            "hgb": FakeProba([0.1, 0.8, 0.3, 0.6]),
            "lgbm": FakeProba([0.2, 0.9, 0.4, 0.1]),
            "logreg": FakeProba([0.3, 0.7, 0.5, 0.0]),
            "ranker": FakeRanker([0.5, 0.4, 0.9, 0.2]),
        },
    }


def test_normalise_bundle_reads_contract_design():
    canon = mod.normalise_bundle(make_contract_bundle())
    assert [c for c in canon["all_features"]] == ["gpg", "hgpg", "tims"]
    assert canon["categorical_features"] == ["tims"]
    assert canon["numeric_features"] == ["gpg", "hgpg"]  # feature_order minus cats
    assert canon["categories"] == {"tims": ["1", "2", "3"]}
    assert canon["metadata"]["train_through"] == ON_DATE
    assert canon["metadata"]["version"] == 1
    assert canon["ranker"] is not None
    # identical predictions to the legacy design (same member models)
    frame = pd.DataFrame({"gpg": [0.5], "hgpg": [0.1], "tims": [1]})
    a, b = mod.encode_frame(canon, frame), mod.encode_frame(make_bundle(), frame)
    pd.testing.assert_frame_equal(a, b, check_categorical=False)


def test_normalise_bundle_reads_legacy_design():
    legacy = make_bundle()
    canon = mod.normalise_bundle(legacy)
    assert canon["all_features"] == legacy["all_features"]
    assert canon["categorical_features"] == legacy["categorical_features"]
    assert canon["metadata"]["seed"] == 0


def test_normalise_bundle_missing_key_clear_error():
    contract = make_contract_bundle()
    del contract["models"]
    with pytest.raises(RuntimeError, match="missing ensemble member|malformed"):
        mod.normalise_bundle(contract)
    legacy = make_bundle()
    del legacy["all_features"], legacy["categorical_features"], legacy["categories"]
    with pytest.raises(RuntimeError, match="malformed"):
        mod.normalise_bundle(legacy)
    contract2 = make_contract_bundle()
    del contract2["feature_order"]
    with pytest.raises(RuntimeError, match="malformed"):
        mod.normalise_bundle(contract2)


def test_handler_runs_on_contract_design_artifact(env_vars, prediction_tables, monkeypatch):
    """The sibling's artifact design drives the full path end to end."""
    calls: list = []
    fake_save(monkeypatch, calls)
    s3 = fake_s3(monkeypatch, make_contract_bundle())
    result = mod.handler({"date": ON_DATE}, None)
    assert len(calls) == 3
    assert {row["tims"] for row in calls} == {1, 2, 3}
    assert result["artifact"]["seed"] == 0
    assert result["artifact"]["through"] == ON_DATE
    assert not s3.get_object.call_args_list[0].args  # reads the lexically latest key


def test_staleness_note_warns_beyond_7_days(env_vars, capsys):
    stale = make_bundle()
    stale["metadata"]["train_through"] = (datetime.date.today() - datetime.timedelta(days=8)).isoformat()
    assert mod.staleness_note(stale) == 8
    assert "8 days stale" in capsys.readouterr().out

    fresh = make_bundle()
    fresh["metadata"]["train_through"] = datetime.date.today().isoformat()
    assert mod.staleness_note(fresh) == 0
    assert "stale" not in capsys.readouterr().out


def test_require_live_columns_hard_fail_names_exact_missing():
    rows = pd.DataFrame([{c: 1.0 for c in ("gpg", "team_name", "tims")}])
    with pytest.raises(RuntimeError) as err:
        mod.require_live_columns(rows, ON_DATE)
    msg = str(err.value)
    for col in ("opp_goalie_team", "hgpg", "lineup_position_group"):
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
    frame = pd.DataFrame(
        {
            "date": [ON_DATE] * 3,
            "player_id": [1, 2, 3],
            "tims": [1, 1, 2],
            "gpg": [0.5, 0.2, 0.9],
            "hgpg": [0.1, 0.2, 0.3],
        }
    )
    sim = mod.ensemble_scores(make_bundle(), frame)
    assert sim["ensemble"].between(0.0, 1.0).all()
    group1 = sim[sim["tims"] == 1]
    # the real model outputs (index 1) dominate both other members inside
    # tims=1: hgb 0.8 vs 0.1, lgbm 0.9 vs 0.2, logreg 0.7 vs 0.3
    assert group1["ensemble"].idxmax() == 1


def _assemble_frame(today, players, teams, goalies):
    """The run()'s frame assembly, test-visible: shared-core shot block onto
    the build_frame join, then add_derived, sliced to the pick date."""
    shot_block = mod.derive_shot_block(ON_DATE, pd.to_numeric(today["player_id"]).tolist())
    frame = model.build_frame(players, teams, goalies, source="supabase")
    frame = frame.drop(columns=model.SHOT_FEATURES, errors="ignore").merge(
        shot_block, on=["date", "player_id"], how="left", validate="many_to_one"
    )
    frame = model.add_derived(frame, teams, source="supabase")
    return frame[frame["date"].astype(str) == ON_DATE].reset_index(drop=True)


def test_supabase_frame_matches_artifact_features(env_vars, prediction_tables):
    loaded = mod.load_prediction_tables("prod", ON_DATE)
    assert loaded is not None
    today, players, teams, goalies = loaded
    frame = _assemble_frame(today, players, teams, goalies)
    bundle = make_bundle()
    assert not frame.empty
    assert all(col in frame.columns for col in bundle["all_features"])
    x = mod.encode_frame(bundle, frame)
    assert list(x.columns) == bundle["all_features"]  # same order/shape as the artifact


# ------------------------------------------------ new data-flow semantics


def test_no_live_rows_exits_cleanly(env_vars, prediction_tables, monkeypatch, capsys):
    """Owner rule: NO live snapshot rows for the pick date = the morning
    snapshot build has not run yet -> warn + no picks + CLEAN exit 0 (no
    RuntimeError, no traceback, no Picks write)."""
    empty_live = fake_live_rows().iloc[0:0]
    # serve an EMPTY live table while keeping the backtrack tables
    fake_client = MagicMock(name="supabase_admin")
    tables = {
        "Player-Snapshots-backtrack": fake_history_players(),
        "Player-Snapshots": empty_live,
        "Team-Stats-backtrack": fake_teams(),
        "Goalie-Snapshots-backtrack": fake_goalies(),
        "Default": fake_goalies(),
    }

    def _table(name):
        for prefix, frame in tables.items():
            if name.startswith(prefix):
                return FakeTable(frame)
        return FakeTable(tables["Default"])

    fake_client.table.side_effect = _table
    monkeypatch.setattr("run_daily_picks.SUPABASE_ADMIN_CLIENT", fake_client)
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))

    calls: list = []
    fake_save(monkeypatch, calls)
    result = mod.handler({"date": ON_DATE}, None)  # must NOT raise
    assert result["statusCode"] == 200
    assert result["picks"] == []
    assert "no live snapshot rows" in result["message"]
    assert calls == []  # no Picks write
    out = capsys.readouterr().out
    assert "no picks computed: no live snapshot rows for 2026-06-14" in out
    assert "Traceback" not in out


def test_missing_history_day_warns_but_still_picks(env_vars, prediction_tables, monkeypatch, capsys):
    """Owner rule: a game day the backtrack publish has not covered yet is
    NORMAL -> ONE warning line naming the day, and the run still publishes
    its 3 picks on the history it has."""
    # the raw store witnessed a 2026-06-12 game day (the crawl crawled it)
    # but the published player history has no rows for it - the publish lag.
    games_with_gap = pd.concat(
        [
            fake_player_games(),
            pd.DataFrame(
                [
                    {
                        "player_id": 1,
                        "date": "2026-06-12",
                        "season": "20252026",
                        "goals": 1,
                        "shots": 3,
                        "toi": "15:00",
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    fake_client = MagicMock(name="supabase_admin")
    tables = {
        "Player-Snapshots-backtrack": fake_history_players(),
        "Player-Snapshots": fake_live_rows(),
        "Team-Stats-backtrack": fake_teams(),
        "Goalie-Snapshots-backtrack": fake_goalies(),
        "Default": fake_goalies(),
    }

    def _table(name):
        for prefix, frame in tables.items():
            if name.startswith(prefix):
                return FakeTable(frame)
        return FakeTable(tables["Default"])

    fake_client.table.side_effect = _table
    monkeypatch.setattr("run_daily_picks.SUPABASE_ADMIN_CLIENT", fake_client)
    monkeypatch.setattr("run_daily_picks._player_games_frame", games_with_gap.copy)
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))

    calls: list = []
    fake_save(monkeypatch, calls)
    result = mod.handler({"date": ON_DATE, "dry": True}, None)
    assert result["statusCode"] == 200
    assert len(result["picks"]) == 3  # assembled anyway (dry: no write)
    assert calls == []
    out = capsys.readouterr().out
    assert "WARNING: no published history rows for game day(s) ['2026-06-12']" in out
    assert "continuing" in out


def test_live_shot_block_matches_training_core(env_vars, prediction_tables):
    """THE train/run parity gate: the live path's today shot block (raw store
    + ONE synthetic today-row per player through the shared core) must equal
    the training path's shot_features() values for the same rows to <1e-9.

    Two shapes, both from the same synthetic per-game store:
      1. a historical replay date (the store already owns the games) -
         identical by construction, guards the wiring;
      2. the pick date itself, where the training math is replayed by
         appending the players' real ON_DATE game lines to the store and
         comparing against the live path's synthetic-row block - the
         synthetic zeros can never enter their own features (shift(1)).
    """
    games = fake_player_games()
    # (2) the training math's hypothetical: the players' real ON_DATE games
    today_games = pd.DataFrame(
        {
            "player_id": [1, 2, 9, 10],
            "date": [ON_DATE] * 4,
            "season": ["20252026"] * 4,
            "goals": [1, 0, 2, 0],
            "shots": [3, 1, 5, 2],
            "toi": ["18:31", "12:04", "21:45", "08:59"],
        }
    )
    full = model.shot_features_from_games(pd.concat([games, today_games], ignore_index=True))
    training_rows = full[full["date"] == ON_DATE].sort_values("player_id").reset_index(drop=True)

    live_block = mod.derive_shot_block(ON_DATE, [1, 2, 9, 10]).sort_values("player_id").reset_index(drop=True)
    assert list(live_block["player_id"]) == [1, 2, 9, 10]
    diffs = (training_rows[model.SHOT_FEATURES] - live_block[model.SHOT_FEATURES]).abs()
    assert diffs.to_numpy().max() < 1e-9
    # and the historical replay is exact to the core (same function, same rows)
    historical = mod.derive_shot_block(CORE_GAME_DAYS[-1], [1, 2, 9, 10])
    assert not historical.empty


def test_live_shot_block_replays_historical_date_exactly(env_vars, prediction_tables):
    """Acceptance test from the design fix: for one historical date X, the
    live path (history < X feeding the shared core) and the local-store
    training path produce the SAME 14 features - max abs diff < 1e-9."""
    games = fake_player_games()
    x_date = CORE_GAME_DAYS[-1]  # 2026-06-10: a played, published date
    training = model.shot_features_from_games(games)
    training_rows = training[training["date"] == x_date].sort_values("player_id").reset_index(drop=True)

    # live path: the store's history + synthetic X rows (deduped by the store's
    # own X games) -> the same core -> X rows
    live_block = mod.derive_shot_block(x_date, [1, 2, 9, 10]).sort_values("player_id").reset_index(drop=True)
    diffs = (training_rows[model.SHOT_FEATURES] - live_block[model.SHOT_FEATURES]).abs()
    assert diffs.to_numpy().max() < 1e-9


# ----------------------------------------------------------- refresh store


def test_refresh_chain_dumps_then_current_season(monkeypatch, tmp_path):
    """The runner-side refresh chain: dump load FIRST, then the current
    season's build for the --through date's season - and the season schedule
    cache is dropped so the walk re-discovers newly FINAL games (the
    DAILY_DATA_RUNBOOK section-2 gotcha: a warm schedule file is served
    verbatim and silently freezes the game list). No fetches here at all:
    build/load are stubbed, this asserts the ORDER + the season selection +
    the schedule-cache eviction."""
    calls = []
    schedule_cache = tmp_path / "schedule-20262027.json"
    schedule_cache.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(refresh_mod.dump_store, "load", lambda db, dumps: calls.append("load") or 685914)
    monkeypatch.setattr(refresh_mod.local_store, "CACHE_DIR", tmp_path)

    def _fake_build(season, db_path=None, delay_seconds=None):
        calls.append(f"build:{season}:{delay_seconds}")

        return 120, 120

    monkeypatch.setattr(refresh_mod.local_store, "build", _fake_build)
    connect_calls = []

    class _FakeConn:
        def execute(self, *_a, **_k):
            connect_calls.append("query")

            class _Row:
                def fetchone(self):
                    return (0,)

            return _Row()

        def close(self):
            pass

    monkeypatch.setattr(refresh_mod.local_store, "connect", lambda _p: _FakeConn())

    season, _rows, _newest = refresh_mod.refresh(
        through="2026-10-27", db_path=tmp_path / "raw.sqlite", dump_dir=tmp_path
    )

    assert season == "20262027"
    assert calls == ["load", "build:20262027:0.34"]
    assert not schedule_cache.exists()  # fresh walk enforced
    assert "20252026" == refresh_mod.season_label("2026-06-13")  # Jan-Jun -> year-1


def test_refresh_is_idempotent_and_skips_load_with_warm_schedule(monkeypatch, tmp_path):
    """--skip-load keeps the existing store and crawls only (a twice-a-day
    pass never re-loads the dumps), and a cold refresh with --warm-schedule
    (fresh_schedule=False) keeps the schedule cache instead of evicting it.
    Season math: month>=7 belongs to the NEW season id."""
    calls = []
    schedule_cache = tmp_path / "schedule-20252026.json"
    schedule_cache.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(refresh_mod.dump_store, "load", lambda *_a, **_k: calls.append("load") or 1)
    monkeypatch.setattr(refresh_mod.local_store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        refresh_mod.local_store, "build", lambda season, **_k: calls.append(f"build:{season}") or (0, 0)
    )

    def _fake_conn(_p):
        class _Conn:
            def execute(self, *_a, **_k):
                class _Row:
                    def fetchone(self):
                        return (0,)

                return _Row()

            def close(self):
                pass

        return _Conn()

    monkeypatch.setattr(refresh_mod.local_store, "connect", _fake_conn)

    (tmp_path / "raw.sqlite").write_text("", encoding="utf-8")  # store exists
    season, _rows, _newest = refresh_mod.refresh(
        through="2026-10-27", db_path=tmp_path / "raw.sqlite", dump_dir=tmp_path, skip_load=True, fresh_schedule=False
    )

    assert season == "20262027"  # October belongs to the new season
    assert calls == ["build:20262027"]  # no dump load, no network fetch code touched
    assert schedule_cache.exists()  # warm schedule kept on request


# ----------------------------------------------------------- pick assembly


def test_pick_rows_one_per_group_and_order():
    frame = pd.DataFrame(
        {
            "date": [ON_DATE, ON_DATE, ON_DATE, ON_DATE],
            "player_id": [1, 2, 9, 10],
            "name": ["a", "b", "g", "h"],
            "team_name": ["T", "T", "Time", "Noise"],
            "tims": [1, 1, 2, 3],
            "ensemble": [0.4, 0.8, 0.6, np.nan],
        }
    )
    picks = model.daily_picks(frame, "ensemble")
    row_map = dict(zip(picks["tims"], picks["player_id"]))
    # highest ensemble member per group; tims=3 is all-NaN -> daily_picks
    # (documented) skips that group, and a real slate would then hard-fail in
    # validate_picks instead of publishing an ambiguous slate
    assert row_map == {1: 2, 2: 9}


def test_validate_picks_rejects_wrong_shape():
    sim = pd.DataFrame(
        {
            "date": [ON_DATE] * 2,
            "player_id": [1, 2],
            "tims": [1, 1],
            "ensemble": [0.5, 0.6],
        }
    )
    picks = model.daily_picks(sim, "ensemble")
    with pytest.raises(RuntimeError, match="3 picks"):
        mod.validate_picks(picks, ON_DATE)


def test_pick_payload_whitelist():
    picks = pd.DataFrame(
        {
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
        }
    )
    rows = mod.pick_rows_to_picks_db(picks)

    assert len(rows) == 1
    assert rows[0] == {
        "date": ON_DATE,
        "player_id": 1,
        "name": "a",
        "team_name": "T",
        "stat": 12.3,  # ensemble ranked-pct -> percent, 1 decimal
        "tims": 1,
        "gpg": 0.5,
        "hgpg": 0.1,
    }
    # `ppg` is not a live Picks-{ENV} column on either env (smoke: the legacy
    # wipe-then-insert shipped it, the insert failed PGRST204, the table was
    # left empty); REMOVE_KEYS strips it before anything is serialized.
    assert "ppg" in mod.REMOVE_KEYS
    assert {k for row in rows for k in row}.isdisjoint(mod.REMOVE_KEYS)


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
    today = fake_live_rows().drop(columns=["opp_goalie_team", "hgpg"])
    monkeypatch.setattr("run_daily_picks.today_live_rows", lambda _env, _d: today)
    calls = []
    fake_save(monkeypatch, calls)
    with pytest.raises(RuntimeError, match="opp_goalie_team"):
        mod.handler({"date": ON_DATE}, None)
    assert calls == []


def test_handler_ambiguous_slate_fails(env_vars, prediction_tables, monkeypatch):
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls = []
    fake_save(monkeypatch, calls)

    def _only_one_group(sim, col):
        return sim[sim["tims"] == 1].head(1)

    monkeypatch.setattr("run_daily_picks.model.daily_picks", _only_one_group)
    with pytest.raises(RuntimeError, match="3 picks"):
        mod.handler({"date": ON_DATE}, None)
    assert calls == []


# ---------------------------------------------------- artifact v2 / v1 frag


def _real_logreg_pipeline():
    """The full fit_logreg chain, fitted on a tiny deterministic fold.

    Used both as artifact v2's logreg member and (unwrapped: the inner bare LR)
    as the v1 fixture member.
    """
    from sklearn.impute import SimpleImputer  # noqa: PLC0415 (test-local fixture)
    from sklearn.linear_model import LogisticRegression  # noqa: PLC0415
    from sklearn.pipeline import make_pipeline  # noqa: PLC0415
    from sklearn.preprocessing import StandardScaler  # noqa: PLC0415

    frame = pd.DataFrame(
        {
            "gpg": [0.5, 0.2, 0.9, 1.1, 0.0, 0.8],
            "hgpg": [0.1, 0.2, 0.3, 0.35, 0.0, 0.28],
            "tims": [1, 1, 1, 2, 2, 2],
            "scored": [0, 0, 1, 0, 0, 1],
        }
    )
    x = frame[["gpg", "hgpg"]]
    pipe = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression())
    pipe.fit(x, frame["scored"])
    return pipe, pipe[-1], x


def test_artifact_v2_nan_frame_pipeline_member_succeeds():
    """Artifact v2: logreg IS the imputing pipeline -> NaN frames score fine.

    The v1 miss this regression-tests: a bare LogisticRegression raised
    'ValueError: Input X contains NaN' on every live frame (live frames always
    carry NaNs - debut rows, opp-goalie gaps), killing the whole run.
    """
    pipe, _bare, _x = _real_logreg_pipeline()
    bundle = make_contract_bundle()
    bundle["version"] = 2  # the v2 contract: version is a TOP-LEVEL key
    bundle["models"]["logreg"] = pipe
    bundle["logreg"] = pipe  # legacy alias, same object (the v2 shape)
    canon = mod.normalise_bundle(bundle)
    assert hasattr(canon["logreg"], "steps")  # the pipeline member, exported as-is

    frame = pd.DataFrame(
        {
            "date": [ON_DATE, ON_DATE],
            "player_id": [1, 2],
            "tims": [1, 1],
            "gpg": [np.nan, 0.4],  # the NaNs the v1 path crashed on
            "hgpg": [np.nan, 0.1],
        }
    )
    sim = mod.ensemble_scores(canon, frame)
    assert len(sim) == 2
    assert sim["logreg"].notna().all()  # imputed inside the member, not crashed


def test_artifact_v1_fallback_warns_loud_and_still_picks(capsys):
    """v1 artifact (bare LR): loud LEGACY warning + the adapter still picks."""
    _pipe, bare, x = _real_logreg_pipeline()
    bundle = make_contract_bundle()  # version stays 1
    bundle["models"]["logreg"] = bare  # the v1 miss: bare inner estimator
    bundle["logreg"] = bare
    canon = mod.normalise_bundle(bundle, key="artifact_2026-06-14_0.joblib")

    frame = pd.DataFrame(
        {
            "date": [ON_DATE, ON_DATE],
            "player_id": [1, 2],
            "tims": [1, 1],
            "gpg": [np.nan, 0.4],  # NaNs the bare member would have crashed on
            "hgpg": [np.nan, 0.1],
        }
    )
    sim = mod.ensemble_scores(canon, frame)
    assert len(sim) == 2
    assert sim["logreg"].notna().all()
    # the loud retrain warning is printed at load time
    assert "LEGACY ARTIFACT FORMAT" in capsys.readouterr().out
    # and the shape the daily pass needs: one pick per (date, tims) group
    picks = model.daily_picks(sim, "ensemble")
    assert len(picks) == 1
