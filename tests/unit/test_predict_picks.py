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
import json
import re
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
# run_daily_picks is loaded ONCE per session under its canonical name: other
# unit files (tests/unit/test_picks_db_nan_behaviors.py) stub the same module
# with the file's fixtures patching by string key ("run_daily_picks.ENV"),
# which only lands if sys.modules["run_daily_picks"] is the SAME object this
# file's `mod` refers to (a second exec_module would clobber the entry and a
# name-keyed patch would land on the wrong module object -> cross-file
# pollution, 26 failures when the files share a session).
_run_dp_path = ROOT / "smartscore" / "scripts" / "backtrack" / "run_daily_picks.py"
mod = sys.modules.get("run_daily_picks")
if not hasattr(mod, "__file__") or Path(mod.__file__).resolve() != _run_dp_path.resolve():
    _SPEC = importlib.util.spec_from_file_location("run_daily_picks", _run_dp_path)  # noqa: E402
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
    # run_daily_picks captures `from config import ENV` at import time, so the
    # setenv above cannot rebind the module global: on CI (no local .env) it
    # stays at config's default 'dev' and the STAGE B gap-fill is invoked with
    # the wrong env. Bind the module global to the same env the fixture declares.
    monkeypatch.setattr(mod, "ENV", "prod")
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
    """The SHARED history ledger Player-Snapshots-backtrack-prod shape (every
    picks env reads that ONE ledger): one row per player-game
    STRICTLY before the pick date (the prior-games context: scored labels,
    club identity, rates), PLUS the stage-2 per-game raw columns."""
    return attach_fake_raw(fake_history_players_base())


def fake_history_players_base():
    """The pre-20261027 frame shape (16 club columns, no raw) - test source
    for the fail-closed migration-col check."""
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


def attach_fake_raw(frame):
    """The stage-2 raw columns for one fake-history frame: each row's OWN
    game stats, matching fake_player_games()'s per-(player, date) lines
    (toi already decimal minutes - what game_toi_min publishes)."""
    games = fake_player_games()
    lookup = {
        (str(row["date"]), int(row["player_id"])): (
            int(row["goals"]),
            int(row["shots"]),
            float(row["toi"].split(":")[0]) + int(row["toi"].split(":")[1]) / 60,
        )
        for row in games.to_dict("records")
    }
    keys = list(zip(frame["date"].astype(str), frame["player_id"].astype(int)))
    missing = [key for key in keys if key not in lookup]
    assert not missing, f"fake raw columns undefined for {missing[:3]}"
    frame = frame.copy()
    frame["game_goals"] = [lookup[key][0] for key in keys]
    frame["game_shots"] = [lookup[key][1] for key in keys]
    frame["game_points"] = [lookup[key][0] for key in keys]  # provenance column; goals==points in the fixture
    frame["game_toi_min"] = [lookup[key][2] for key in keys]
    return frame


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

    # The Picks-{ENV} slate (stat_update's writable surface): a mutable
    # FakePicksTable reachable from the tests via the fake client.
    picks_table = FakePicksTable(fake_picks_slate())

    fake_client = MagicMock(name="supabase_admin")

    def _table(name):
        if name.startswith("Picks-"):
            return picks_table
        for prefix, frame in tables.items():
            if name.startswith(prefix):
                return FakeTable(frame)
        return FakeTable(tables["Default"])

    fake_client.table.side_effect = _table
    fake_client.picks_table = picks_table
    monkeypatch.setattr("run_daily_picks.SUPABASE_ADMIN_CLIENT", fake_client)
    # the gap-fill helper shares the admin client; patch it too so a run()'s
    # first-step invocation (and the dry witness) never leaves the fakes.
    monkeypatch.setattr("backfill_missing_days.SUPABASE_ADMIN_CLIENT", fake_client)
    # the first-step backfill itself is patched to a no-op summary here; the
    # wiring tests below re-patch it with a call recorder.
    monkeypatch.setattr(
        "run_daily_picks.backfill_missing_days.backfill",
        lambda *a, **k: {
            "status": "up-to-date",
            "games": 0,
            "rows": 0,
            "created": 0,
            "backfilled": [],
            "skipped": [],
            "failed_days": [],
            "pending": [],
        },
    )

    # the stage-2 raw columns are "landed" on the fake table: the exact
    # recompute reads the published record (fake_history_players' raw block),
    # no local store exists or is needed.
    monkeypatch.setattr("run_daily_picks._RAW_PROBE", {"prod": True})
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


class FakePicksTable:
    """The Picks-{ENV} builder chain the stat_update writer uses:
    select(.eq date) / update(.eq id).execute().

    Records every PATCH in ``.patches`` as (row_id, payload, before-row
    copy), so the tests can assert the write touched ONLY the stat column
    of the EXISTING rows (mutation on the shared row dicts makes the
    before/after compare trivial).
    """

    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.patches = []
        self._eq = None
        self._pending = None

    def select(self, *_cols, **_kw):
        return self

    def update(self, payload):
        self._pending = dict(payload)
        return self

    def eq(self, col, value):
        self._eq = (col, value)
        return self

    def execute(self):
        if self._pending is not None:
            col, value = self._eq or (None, None)
            for row in self.rows:
                if str(row.get(col)) == str(value):
                    self.patches.append((row["id"], dict(self._pending), dict(row)))
                    row.update(self._pending)
        self._eq = None
        self._pending = None
        return FakeResult(pd.DataFrame(self.rows))


# The live Picks-{ENV} column set (test_picks_db_nan_behaviors.PICKS_COLUMNS):
# the site's OWN full-slate publish (utility.save_to_db) wrote these rows for
# the pick date - the stat_update writer PATCHes only their stat.
def fake_picks_slate():
    return [
        {
            "id": row_id,
            "date": ON_DATE,
            "player_id": player_id,
            "name": name,
            "team_name": team,
            "stat": None,
            "tims": tims,
            "gpg": gpg,
            "hgpg": hgpg,
        }
        for row_id, player_id, name, team, tims, gpg, hgpg in (
            (1, 1, "a", "Toronto", 1, 0.5, 0.1),
            (2, 2, "b", "Toronto", 1, 0.2, 0.2),
            (3, 9, "g", "Montréal", 2, 0.0, 0.0),
            (4, 10, "h", "Montréal", 3, 0.0, 0.0),
        )
    ]


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
    result = mod.handler({"date": ON_DATE, "write_mode": "legacy"}, None)
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


def _assemble_frame(today, players, teams, goalies, history):
    """The run()'s frame assembly, test-visible: the shared-core shot block
    over the published per-game record, onto the build_frame join, then
    add_derived, sliced to the pick date."""
    shot_block = mod.shot_block_from_history(ON_DATE, pd.to_numeric(today["player_id"]).tolist(), history)
    frame = model.build_frame(players, teams, goalies, source="supabase")
    frame = frame.drop(columns=model.SHOT_FEATURES, errors="ignore").merge(
        shot_block, on=["date", "player_id"], how="left", validate="many_to_one"
    )
    frame = model.add_derived(frame, teams, source="supabase")
    return frame[frame["date"].astype(str) == ON_DATE].reset_index(drop=True)


def test_supabase_frame_matches_artifact_features(env_vars, prediction_tables):
    loaded = mod.load_prediction_tables("prod", ON_DATE)
    assert loaded is not None
    today, players, teams, goalies, history = loaded
    frame = _assemble_frame(today, players, teams, goalies, history)
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
    monkeypatch.setattr("run_daily_picks._RAW_PROBE", {"prod": True})
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
    """Owner rule: a game day the published record does not cover is NORMAL ->
    ONE warning line naming the day, and the run still assembles its 3 picks
    on the history it has (missing-day semantics: no hard-fail)."""
    # HISTORY_DATE (2026-06-10) is published but carries NO per-game raw
    # stats (a row predating the one-shot top-up): its games cannot enter
    # the shift(1)/rolling math - warn + continue.
    history_gapped = fake_history_players()
    gap_day = history_gapped["date"] == HISTORY_DATE
    history_gapped.loc[gap_day, ["game_goals", "game_shots", "game_toi_min"]] = pd.NA
    fake_client = MagicMock(name="supabase_admin")
    tables = {
        "Player-Snapshots-backtrack": history_gapped,
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
    monkeypatch.setattr("run_daily_picks._RAW_PROBE", {"prod": True})
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))

    calls: list = []
    fake_save(monkeypatch, calls)
    result = mod.handler({"date": ON_DATE, "dry": True}, None)
    assert result["statusCode"] == 200
    assert len(result["picks"]) == 3  # assembled anyway (dry: no write)
    assert calls == []
    out = capsys.readouterr().out
    assert "WARNING: 1 published game day(s) inside the lookback window carry no per-game raw stat(s)" in out
    assert HISTORY_DATE in out
    assert "continuing" in out


def test_live_shot_block_matches_training_core(env_vars, prediction_tables):
    """THE train/run parity gate: the live path's today shot block (the
    PUBLISHED per-game record + ONE synthetic today-row per player through
    the shared core) must equal the training path's shot_features() values
    for the same rows to <1e-9.

    Two shapes:
      1. a historical replay date (the record already covers the games) -
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

    history = mod.load_prediction_tables("prod", ON_DATE)[4]
    live_block = (
        mod.shot_block_from_history(ON_DATE, [1, 2, 9, 10], history).sort_values("player_id").reset_index(drop=True)
    )
    assert list(live_block["player_id"]) == [1, 2, 9, 10]
    diffs = (training_rows[model.SHOT_FEATURES] - live_block[model.SHOT_FEATURES]).abs()
    assert diffs.to_numpy().max() < 1e-9
    # and the historical replay is exact to the core (same function, same rows)
    historical = mod.shot_block_from_history(CORE_GAME_DAYS[-1], [1, 2, 9, 10], history)
    assert not historical.empty


def test_live_shot_block_replays_historical_date_exactly(env_vars, prediction_tables):
    """Acceptance test from the design fix: for one historical date X, the
    live path (the published per-game record < X feeding the shared core)
    and the local-store training path produce the SAME 14 features - max
    abs diff < 1e-9."""
    games = fake_player_games()
    x_date = CORE_GAME_DAYS[-1]  # 2026-06-10: a played, published date
    training = model.shot_features_from_games(games)
    training_rows = training[training["date"] == x_date].sort_values("player_id").reset_index(drop=True)

    history = mod.load_prediction_tables("prod", x_date)[4]
    live_block = (
        mod.shot_block_from_history(x_date, [1, 2, 9, 10], history).sort_values("player_id").reset_index(drop=True)
    )
    diffs = (training_rows[model.SHOT_FEATURES] - live_block[model.SHOT_FEATURES]).abs()
    assert diffs.to_numpy().max() < 1e-9


def test_season_label_matches_the_store_keying_incl_covid():
    """The published record carries no season column, so season_label IS the
    season key the recompute groups on - it must reproduce the raw store's
    ids. The two COVID-era post-seasons are the trap: the 2019-20 bubble
    (Jul-Sep 2020, keyed 20192020) and the delayed 2020-21 post-season
    (July 2021, keyed 20202021) - a plain month rule mislabels both and
    shifts every season-scoped feature for the veterans who played them
    (verified against the store: 56 distinct game dates)."""
    assert mod.season_label("2026-10-24") == "20262027"
    assert mod.season_label("2026-06-13") == "20252026"
    assert mod.season_label("2020-08-01") == "20192020"  # the bubble
    assert mod.season_label("2021-07-07") == "20202021"  # the delayed 2020-21 final
    assert mod.season_label("2022-06-26") == "20212022"  # a normal June final


def test_history_rows_reads_the_shared_ledger_on_a_dev_run(env_vars, monkeypatch):
    """(c) A dev-mode run's history_rows reads the SHARED ledger
    (Player-Snapshots-backtrack-prod) - never the deleted `-dev` backtrack
    name the old suffix routing resolved; only the LIVE table stays
    env-resolved. Strictly-prior semantics unchanged (same function core)."""
    seen_tables = []
    history = fake_history_players()

    def _spy_window(table, _columns, _first, before_date, _keys):
        seen_tables.append((table, before_date))
        old = history[history["date"].astype(str) < str(before_date)]
        return old.reset_index(drop=True)

    monkeypatch.setattr(mod, "_paged_date_window", _spy_window)
    monkeypatch.setattr(mod, "_first_published_date", lambda table: "2013-10-01")
    monkeypatch.setattr(mod, "_RAW_PROBE", {"dev": True})
    frame = mod.history_rows("dev", ON_DATE)
    assert seen_tables == [("Player-Snapshots-backtrack-prod", ON_DATE)]
    assert not frame.empty  # strictly-before rows fetched whole, as before
    # the deleted shell never resolves anywhere in the module's routing
    assert "Player-Snapshots-backtrack-dev" not in " ".join(t for t, _b in seen_tables)


def test_dev_run_history_reads_route_to_the_shared_ledger(env_vars, monkeypatch):
    """(c) Wiring: a picks env of dev still routes EVERY history read
    (players/teams/goalies) to the SHARED `-prod` backtrack tables; the only
    env-resolved reads are the LIVE snapshot (`Player-Snapshots-dev`) and,
    later, `Picks-dev`. Spied at the module's own routing layer
    (_paged_date_window / _first_published_date / today_live_rows) so the
    assertion does not depend on which run_daily_picks module instance the
    fakes were wired to."""
    routed = []
    live_reads = []
    live = fake_live_rows()
    history = fake_history_players()

    def _frame_for(table):
        if table.startswith("Player-Snapshots-backtrack"):
            return history
        if table.startswith("Team-Stats"):
            return fake_teams()
        return fake_goalies()

    def _window(table, _cols, _first, _before, _keys):
        routed.append(table)
        return _frame_for(table)

    monkeypatch.setattr(mod, "_paged_date_window", _window)
    monkeypatch.setattr(mod, "_first_published_date", lambda table: "2013-10-01")
    monkeypatch.setattr(mod, "_RAW_PROBE", {"dev": True})

    def _live_spy(env, on_date):
        live_reads.append(mod.live_player_table(env))
        return live

    monkeypatch.setattr(mod, "today_live_rows", _live_spy)
    today, _players, _teams, _goalies, history_out = mod.load_prediction_tables("dev", ON_DATE)
    assert today is not None and not history_out.empty
    assert live_reads == ["Player-Snapshots-dev"]  # the LIVE read IS env-resolved
    assert routed == ["Team-Stats-backtrack-prod", "Goalie-Snapshots-backtrack-prod", "Player-Snapshots-backtrack-prod"]
    assert all(n.endswith("-prod") for n in routed)  # SHARED ledgers only
    assert "Player-Snapshots-backtrack-dev" not in routed  # the deleted shell never resolves


def test_history_rows_fail_closed_without_raw_columns(env_vars, monkeypatch):
    """The stage-2 gate: a picks run reaching the read BEFORE the raw-column
    migration has landed fails LOUDLY, naming the migration file - not a
    silent stage-1-style feature approximation. (The fake client rejects the
    `game_shots` probe the way a real PostgREST 400s an unknown column.)"""
    fake_client = MagicMock(name="supabase_admin")
    probe = MagicMock(name="client_table")
    probe.select.side_effect = Exception("PGRST204: column game_shots does not exist")
    fake_client.table.return_value = probe
    monkeypatch.setattr("run_daily_picks.SUPABASE_ADMIN_CLIENT", fake_client)
    monkeypatch.setattr("run_daily_picks._RAW_PROBE", {})
    with pytest.raises(RuntimeError, match="20261027_add_per_game_raw_columns_player_snapshots_backtrack"):
        mod.history_rows("prod", ON_DATE)


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
    """LEGACY write mode: the 3 challenge picks ARE the Picks write (pinned so
    the demoted wipe-then-publish path keeps working behind --write-mode
    legacy; the DEFAULT write mode is stat_update - see the tests below)."""
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls = []
    fake_save(monkeypatch, calls)
    result = mod.handler({"date": ON_DATE, "write_mode": "legacy"}, None)
    assert result["statusCode"] == 200 and not result["dry"]
    assert len(calls) == 3 and sorted(r["tims"] for r in calls) == [1, 2, 3]
    assert all(r["date"] == ON_DATE and r["stat"] is not None for r in calls)


def test_handler_dry_skips_write(env_vars, prediction_tables, monkeypatch):
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls = []
    fake_save(monkeypatch, calls)
    result = mod.handler({"date": ON_DATE, "dry": True}, None)
    assert result["dry"] and calls == []


def test_handler_first_step_backfill_invoked_once_when_not_dry(env_vars, prediction_tables, monkeypatch):
    """STAGE B wiring: a NOT-dry run's FIRST data step is the daily gap-fill,
    invoked once with --env ENV --through-date (the pick date's yesterday)."""
    invocations = []

    def _record(env, through_date=None, **_k):
        invocations.append((env, through_date))
        return {
            "status": "ok",
            "games": 1,
            "rows": 4,
            "created": 1,
            "backfilled": ["2026-06-13"],
            "skipped": [],
            "failed_days": [],
            "pending": [],
        }

    monkeypatch.setattr("run_daily_picks.backfill_missing_days.backfill", _record)
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls = []
    fake_save(monkeypatch, calls)
    result = mod.handler({"date": ON_DATE, "write_mode": "legacy"}, None)
    assert result["statusCode"] == 200
    assert invocations == [("prod", "2026-06-13")]  # exactly once, yesterday ET of the pick date


def test_handler_dry_skips_the_backfill_entirely(env_vars, prediction_tables, monkeypatch, capsys):
    """A dry pass is read-only end to end: the gap-fill NEVER runs (no
    writes, no crawl); the run only LISTS the missing game days."""
    invocations = []

    def _record(*_a, **_k):
        invocations.append(1)
        return {"status": "ok", "failed_days": []}

    monkeypatch.setattr("run_daily_picks.backfill_missing_days.backfill", _record)
    # the read-only witness: latest published date well behind the pick date
    monkeypatch.setattr("run_daily_picks.backfill_missing_days.latest_published_date", lambda env: "2026-06-01")
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls = []
    fake_save(monkeypatch, calls)
    result = mod.handler({"date": ON_DATE, "dry": True}, None)
    assert result["dry"] and calls == [] and invocations == []
    out = capsys.readouterr().out
    assert "WARNING (dry)" in out and "not backfilled" in out


def test_handler_backfill_failure_still_picks(env_vars, prediction_tables, monkeypatch, capsys):
    """A partial/failed gap-fill is LOUD but never fatal: the exact recompute
    proceeds over the history that is published and the picks still land."""

    def _boom(*_a, **_k):
        raise RuntimeError("supabase down")

    monkeypatch.setattr("run_daily_picks.backfill_missing_days.backfill", _boom)
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls = []
    fake_save(monkeypatch, calls)
    result = mod.handler({"date": ON_DATE, "write_mode": "legacy"}, None)
    assert result["statusCode"] == 200 and len(calls) == 3
    assert "gap-fill backfill failed outright" in capsys.readouterr().out


def test_handler_carry_forward_fallback_only_when_exact_fails(env_vars, prediction_tables, monkeypatch, capsys):
    """The stage-1 carry-forward attach survives ONLY as the clearly-marked
    last-resort fallback: when the exact recompute raises, the run attaches
    the last published row's precomputed rolling columns and STILL picks -
    with a LOUD warning naming the fallback."""
    monkeypatch.setattr(
        "run_daily_picks.shot_block_from_history",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("core exploded")),
    )
    # the fallback's lazy read: the players' latest published rolling row
    latest = fake_history_players().sort_values("date").groupby("player_id").tail(1)
    for col in model.SHOT_FEATURES:
        latest[col] = 0.5
    monkeypatch.setattr(mod, "_latest_shot_columns", lambda env, ids: latest)
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls = []
    fake_save(monkeypatch, calls)
    result = mod.handler({"date": ON_DATE, "write_mode": "legacy"}, None)
    assert result["statusCode"] == 200 and len(calls) == 3
    out = capsys.readouterr().out
    assert "STAGE-1 carry-forward attach" in out


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
        mod.handler({"date": ON_DATE, "write_mode": "legacy"}, None)
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


# ------------------------------------------------------ explicit ENV resolution
# The ml-picks.yml safety contract: the workflow resolves ENV explicitly
# (scheduled = the job-level env: ENV: prod ONLY; manual dispatch = the
# `environment` input override at the "Resolve the picks ENV" step) and binds
# the resolved value as a step-level ENV on both script steps - GitHub Actions
# puts it in the PROCESS environment before `python` starts, so config.py's
# import-time read (config.py:12, ENV = os.environ.get("ENV", "dev")) is the
# single, final resolution. These tests pin the import math of that contract:
# a FRESH config + run_daily_picks import sees exactly the os.environ value
# present before import, and save_picks_db routes every surface (schema probe,
# prior read, wipe, upsert, verify) to Picks-{ENV} - so a workflow run cannot
# silently write the wrong Picks table.


class _EnvProbeTable:
    """Minimal supabase-py builder-chain fake for save_picks_db's five
    surfaces: the schema probe (select(col-csv).limit(1) MUST succeed =
    prove-before-delete), the prior-row read (select '*'), the wipe
    (delete().neq), the upsert, and the post-write id verify."""

    def __init__(self, state):
        self._state = state

    def select(self, *_columns):
        self._state["result"] = list(self._state["rows"])
        return self

    def limit(self, _n):
        return self

    def delete(self):
        return self

    def neq(self, _col, _value):
        self._state["rows"] = []
        return self

    def upsert(self, rows):
        self._history_upserted = [dict(row) for row in rows]
        self._state.setdefault("upserts", []).extend(self._history_upserted)
        self._state["rows"] = [dict(row) for row in rows]
        return self

    def execute(self):
        return types.SimpleNamespace(data=list(self._state.get("result", [])))


class _EnvProbeClient:
    """Name-recording client: every .table() call records the table name, so
    the test asserts ALL of the write path's surfaces hit the SAME table."""

    def __init__(self):
        self.tables = {}

    def table(self, name):
        self._state = self.tables.setdefault(name, {"rows": [], "result": [], "upserts": []})
        return _EnvProbeTable(self._state)


def _fresh_import_with_env(monkeypatch, env_value=None):
    """Import a SECOND config + run_daily_picks copy with os.environ.ENV set
    (or absent) BEFORE import - the GitHub Actions contract above. `config` is
    swapped in sys.modules for a fresh copy because a cached `from config
    import ENV` would rebind the STALE import-time value instead of the one
    the fresh process env would give; monkeypatch restores the real modules
    after the test. load_dotenv is neutralized so a local .env (which carries
    ENV=prod on owner machines) cannot shadow the env-under-test the way CI
    (no .env) sees it."""
    if env_value is None:
        monkeypatch.delenv("ENV", raising=False)
    else:
        monkeypatch.setenv("ENV", env_value)
    monkeypatch.setattr(sys.modules["dotenv"], "load_dotenv", lambda *args, **kwargs: False)

    spec = importlib.util.spec_from_file_location(
        f"config_envprobe_{env_value or 'unset'}", ROOT / "smartscore" / "config.py"
    )
    fresh_config = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "config", fresh_config)
    spec.loader.exec_module(fresh_config)

    name = f"run_daily_picks_envprobe_{env_value or 'unset'}"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "smartscore" / "scripts" / "backtrack" / "run_daily_picks.py"
    )
    fresh = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, fresh)
    spec.loader.exec_module(fresh)
    return fresh


@pytest.mark.parametrize("env_value", [None, "dev", "prod"])
def test_env_resolved_at_import_routes_the_picks_write(monkeypatch, env_value):
    """The ENV import math: unset -> config's 'dev' default; ENV=prod preset ->
    'prod'. Each fresh module then runs the SAvE path (save_picks_db) and
    every surface - schema probe, prior read, wipe, upsert, verify - must
    resolve to Picks-{env}. The env_vars fixture's module-global monkeypatch
    path (mod.ENV rebind after import) remains covered by the tests above."""
    fresh = _fresh_import_with_env(monkeypatch, env_value)
    expected_env = env_value or "dev"

    # the workflow res ENV is visible module-side exactly as the runner set it
    assert fresh.ENV == expected_env

    client = _EnvProbeClient()
    monkeypatch.setattr(fresh, "SUPABASE_ADMIN_CLIENT", client)
    table = f"Picks-{expected_env}"
    rows = [{"player_id": 123, "tims": 2, "stat": 51.0, "date": "2026-06-15"}]

    fresh.save_picks_db(rows, "2026-06-15")

    # ALL surfaces (probe/prior/wipe/upsert/verify) targeted the SAME table
    assert set(client.tables) == {table}, f"save path touched {set(client.tables)}, not purely {table}"
    payload = client.tables[table]["upserts"]
    assert [row["id"] for row in payload] == [1]
    assert payload[0]["player_id"] == 123


# ------------------------------------------------- hybrid mode (source/write)
# The hybrid axes (run_daily_picks.run + --source-env/--history-env):
#   WRITE env -> module-global ENV (config.ENV) - Picks-{ENV};
#   SOURCE env -> --source-env / SOURCE_ENV var -> ENV (today_live_rows);
#   HISTORY env -> --history-env (default 'prod' pinned) -> the ONE shared
#   -prod ledger REGARDLESS (027ef47). The owner's pre-deployment
#   validation profile: ENV=dev + --source-env prod (prod live slate,
#   shared history, writes ONLY Picks-dev).


def record_table_names(fake_client):
    """Wrap a fake client's table() side_effect to record every table name
    a run resolves (the routing witness for the env-axis tests)."""
    names = []
    real = fake_client.table.side_effect

    def _recorder(name):
        names.append(name)
        return real(name)

    fake_client.table.side_effect = _recorder
    return names


def test_hybrid_env_dev_source_prod_reads_prod_snapshot(env_vars, prediction_tables, monkeypatch, capsys):
    """(a)+(b) THE HYBRID: ENV=dev (write side) + event source_env=prod -
    today's rows are read from Player-Snapshots-prod (the REAL prod slate),
    every history read still routes to the SHARED -prod ledger, and
    Picks-dev is never read (the Picks write is a write-only surface and
    this is a dry pass). Default-unset behavior is asserted separately."""
    monkeypatch.setattr(mod, "ENV", "dev")  # the WRITE side of the hybrid
    monkeypatch.delenv("SOURCE_ENV", raising=False)
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls: list = []
    fake_save(monkeypatch, calls)
    names = record_table_names(prediction_tables)

    result = mod.handler({"date": ON_DATE, "dry": True, "source_env": "prod", "write_mode": "legacy"}, None)

    assert result["statusCode"] == 200
    assert len(result["picks"]) == 3 and calls == []  # dry: assembled, no write
    assert "Player-Snapshots-prod" in names  # the LIVE read IS source-resolved
    assert "Player-Snapshots-dev" not in names  # the dev live table never touched
    history_tables = [n for n in names if n != "Player-Snapshots-prod"]
    assert not [n for n in history_tables if "Picks-" in n]
    # (b) EVERY history read routes to the shared -prod ledger (027ef47)
    for shared in (
        "Player-Snapshots-backtrack-prod",
        "Team-Stats-backtrack-prod",
        "Goalie-Snapshots-backtrack-prod",
    ):
        assert shared in names
    assert all(n.endswith("-prod") for n in names)  # one data plane, unchanged
    assert "Player-Snapshots-backtrack-dev" not in names
    out = capsys.readouterr().out
    assert "env axes: write=Picks-dev write_mode=legacy | live read=Player-Snapshots-prod" in out
    picks_line = [line for line in out.splitlines() if "SMARTSCORE_ML_PICKS" in line][0]
    payload = json.loads(picks_line)
    assert payload["write_env"] == "dev" and payload["source_env"] == "prod"


def test_hybrid_source_env_env_var_axis_wins_over_fallback(env_vars, prediction_tables, monkeypatch, capsys):
    """(a) The SOURCE_ENV env var is honored without an event/CLI value - the
    workflow passes the resolved source both ways (step env + CLI flag);
    either alone drives the live read."""
    monkeypatch.setattr(mod, "ENV", "dev")
    monkeypatch.setenv("SOURCE_ENV", "prod")
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls: list = []
    fake_save(monkeypatch, calls)
    names = record_table_names(prediction_tables)

    result = mod.handler({"date": ON_DATE, "dry": True}, None)  # NO source_env key

    assert result["statusCode"] == 200 and len(result["picks"]) == 3
    assert "Player-Snapshots-prod" in names
    assert "Player-Snapshots-dev" not in names
    out = capsys.readouterr().out
    assert "live read=Player-Snapshots-prod" in out


def test_hybrid_default_unset_source_resolves_prod(env_vars, prediction_tables, monkeypatch, capsys):
    """(c) Unset source + a PROD write-side env resolves exactly as before:
    no --source-env, no SOURCE_ENV var, ENV=prod -> the live read is
    Player-Snapshots-prod (the source-of-truth default) and nothing else
    changes."""
    monkeypatch.delenv("SOURCE_ENV", raising=False)
    monkeypatch.delenv("HISTORY_ENV", raising=False)
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls: list = []
    fake_save(monkeypatch, calls)
    names = record_table_names(prediction_tables)

    result = mod.handler({"date": ON_DATE, "dry": True}, None)

    assert result["statusCode"] == 200 and len(result["picks"]) == 3
    assert "Player-Snapshots-prod" in names  # the source-of-truth default
    out = capsys.readouterr().out
    picks_line = [line for line in out.splitlines() if "SMARTSCORE_ML_PICKS" in line][0]
    payload = json.loads(picks_line)
    assert payload["write_env"] == "prod" and payload["source_env"] == "prod" and payload["history_env"] == "prod"


def test_dispatch_matrix_unset_source_with_dev_write_resolves_prod(env_vars, prediction_tables, monkeypatch, capsys):
    """(a) THE OWNER'S CASE (behavior change): an environment=dev dispatch
    with an UNSET source runs EXACTLY the pre-hybrid-safe dispatch path the
    workflow's resolve step passes (no source_env key, no SOURCE_ENV var,
    ENV=dev) - and the live read resolves to PROD. The OLD fallback (source
    follows the write env) silently read the EMPTY Player-Snapshots-dev
    shell here and clean-skipped; now: Player-Snapshots-prod is the live
    read, Picks-dev is the (dry) write side, and Picks-prod stays PROD-prod
    untouched."""
    monkeypatch.setattr(mod, "ENV", "dev")  # environment=dev (WRITE side only)
    monkeypatch.delenv("SOURCE_ENV", raising=False)
    monkeypatch.delenv("HISTORY_ENV", raising=False)
    monkeypatch.setattr("run_daily_picks._RAW_PROBE", {"dev": True, "prod": True})
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls: list = []
    fake_save(monkeypatch, calls)
    names = record_table_names(prediction_tables)

    result = mod.handler({"date": ON_DATE, "dry": True}, None)  # NO source_env key

    assert result["statusCode"] == 200 and len(result["picks"]) == 3
    assert "Player-Snapshots-prod" in names  # the live read IS prod...
    assert "Player-Snapshots-dev" not in names  # ...never the empty dev shell
    out = capsys.readouterr().out
    picks_line = [line for line in out.splitlines() if "SMARTSCORE_ML_PICKS" in line][0]
    payload = json.loads(picks_line)
    assert payload["write_env"] == "dev" and payload["source_env"] == "prod" and payload["history_env"] == "prod"
    assert "live read=Player-Snapshots-prod" in out


def test_dispatch_matrix_explicit_source_dev_overrides(env_vars, prediction_tables, monkeypatch, capsys):
    """(b) The legacy override: an EXPLICIT source=dev (event source_env on
    the resolved-axes contract, i.e. the workflow's explicitly-set
    source=dev input) still resolves the live read to
    Player-Snapshots-dev (the empty shell in real life - this is the
    dev-side testing profile)."""
    monkeypatch.setattr(mod, "ENV", "dev")
    monkeypatch.delenv("SOURCE_ENV", raising=False)
    monkeypatch.setattr("run_daily_picks._RAW_PROBE", {"dev": True, "prod": True})
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls: list = []
    fake_save(monkeypatch, calls)
    names = record_table_names(prediction_tables)

    result = mod.handler({"date": ON_DATE, "dry": True, "source_env": "dev"}, None)

    assert result["statusCode"] == 200 and len(result["picks"]) == 3
    assert "Player-Snapshots-dev" in names  # the explicit override won
    out = capsys.readouterr().out
    picks_line = [line for line in out.splitlines() if "SMARTSCORE_ML_PICKS" in line][0]
    payload = json.loads(picks_line)
    assert payload["write_env"] == "dev" and payload["source_env"] == "dev" and payload["history_env"] == "prod"
    assert "live read=Player-Snapshots-dev" in out


def test_resolve_env_axis_resolution_order(monkeypatch):
    """The documented CLI->env-var->fallback order, whitelisted fail-closed:
    a bad axis value aborts BEFORE any read (ValueError), like the workflow's
    resolve step."""
    monkeypatch.delenv("SOURCE_ENV", raising=False)
    monkeypatch.delenv("HISTORY_ENV", raising=False)
    # fallback = SOURCE_ENV_DEFAULT 'prod' unconditionally (the source-of-
    # truth rule: run() passes SOURCE_ENV_DEFAULT, NEVER the write env -
    # the pre-20261103 caller passed ENV and re-created the silent
    # Player-Snapshots-dev clean-skip on an environment=dev run)
    assert mod.resolve_env_axis(None, "SOURCE_ENV", mod.SOURCE_ENV_DEFAULT) == "prod"
    # env var beats the fallback, CLI beats the env var (the legacy dev
    # live-read override)
    monkeypatch.setenv("SOURCE_ENV", "dev")
    assert mod.resolve_env_axis(None, "SOURCE_ENV", mod.SOURCE_ENV_DEFAULT) == "dev"
    assert mod.resolve_env_axis("prod", "SOURCE_ENV", "dev") == "prod"
    # history axis: the 'prod' default is what run() passes (pinned)
    assert mod.resolve_env_axis(None, "HISTORY_ENV", mod.HISTORY_ENV_DEFAULT) == "prod"
    monkeypatch.setenv("HISTORY_ENV", "dev")  # accepted value (routing still shared)
    assert mod.resolve_env_axis(None, "HISTORY_ENV", "prod") == "dev"
    # fail-closed whitelist (delete the env var so the fallback is consulted)
    with pytest.raises(ValueError, match="axis: SOURCE_ENV"):
        mod.resolve_env_axis("staging", "SOURCE_ENV", "prod")
    monkeypatch.delenv("SOURCE_ENV", raising=False)
    monkeypatch.setenv("SOURCE_ENV", "")
    with pytest.raises(ValueError):
        mod.resolve_env_axis(None, "SOURCE_ENV", "staging")


def test_cli_source_and_history_flags_map_into_the_event(monkeypatch):
    """The CLI wiring: --source-env/--history-env ride the event; unset flags
    add NO key (the run() fallbacks then apply)."""
    events = []
    monkeypatch.setattr(mod, "run", lambda event, context=None: (events.append(event), {"statusCode": 200})[1])

    mod.main(["--date", "2026-10-23", "--source-env", "prod", "--dry"])
    assert events[0]["source_env"] == "prod"
    assert "history_env" not in events[0]  # default: no flag, no key

    mod.main(["--date", "2026-10-23", "--history-env", "dev"])
    assert events[1]["history_env"] == "dev"
    assert "source_env" not in events[1]

    with pytest.raises(SystemExit):
        mod.main(["--source-env", "staging"])


def test_hybrid_run_writes_picks_dev_with_prod_source(monkeypatch, capsys):
    """(c) On the fresh-import surface (the workflow's exact import math): a
    hybrid event (source_env=prod) resolves the READ axis to prod while the
    WRITE global stays config.ENV ('dev') - and the real save path
    (save_picks_db) targets Picks-dev on every surface."""
    fresh = _fresh_import_with_env(monkeypatch, "dev")
    # the resolution order on the fresh module: READ axis -> prod (explicit),
    # WRITE env -> untouched 'dev' global (no CLI re-bind by design).
    assert fresh.ENV == "dev"
    assert fresh.resolve_env_axis("prod", "SOURCE_ENV", fresh.ENV) == "prod"

    client = _EnvProbeClient()
    monkeypatch.setattr(fresh, "SUPABASE_ADMIN_CLIENT", client)
    rows = [{"player_id": 123, "tims": 2, "stat": 51.0, "date": "2026-06-15"}]
    fresh.save_picks_db(rows, "2026-06-15")
    assert set(client.tables) == {"Picks-dev"}, f"hybrid write touched {set(client.tables)}"
    assert [row["id"] for row in client.tables["Picks-dev"]["upserts"]] == [1]


# -------------------------------------------------- stat_update (DEFAULT) mode


def picks_log_line(out):
    return json.loads([line for line in out.splitlines() if "SMARTSCORE_ML_PICKS" in line][0])


def test_default_write_mode_is_stat_update_and_fills_only_stat(env_vars, prediction_tables, monkeypatch, capsys):
    """The owner-corrected contract: the DEFAULT write mode PATCHes ONLY the
    stat column on the EXISTING Picks-{ENV} slate rows - row count, ids and
    every other column identical before/after, no save_picks_db call, and
    the top-3 challenge picks emitted as log output only."""
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    picks = prediction_tables.picks_table
    before = [dict(row) for row in picks.rows]  # the pre-run slate, including stat=None

    calls: list = []
    fake_save(monkeypatch, calls)
    result = mod.handler({"date": ON_DATE}, None)

    assert result["statusCode"] == 200
    assert result["write_mode"] == "stat_update"
    assert result["stat_update"] == {"slate_rows": 4, "filled": 4, "no_frame": 0, "not_on_slate": 0, "skipped": False}
    # (a) ONLY the stat column moved, on the EXISTING rows (no insert, no wipe)
    assert len(picks.rows) == len(before) == 4
    assert [row["id"] for row in picks.rows] == [row["id"] for row in before]
    for after, pre in zip(picks.rows, before):
        for col, value in after.items():
            if col == "stat":
                assert pre["stat"] is None and isinstance(value, float) and 0.0 <= value <= 100.0
            else:
                assert value == pre[col], f"column {col} mutated: {pre[col]!r} -> {value!r}"
    # four PATCHes, each carrying ONLY the stat key, one per existing id
    assert [row_id for row_id, _payload, _pre in picks.patches] == [1, 2, 3, 4]
    assert all(set(payload) == {"stat"} for _i, payload, _p in picks.patches)
    # the ensemble mean-of-rank-pct -> percent, 1 decimal convention
    sim_pct = round(int(picks.patches[0][1]["stat"]), 1)
    assert sim_pct == picks.patches[0][1]["stat"]
    # (e) the top-3 challenge picks are an OUTPUT: logged, never written
    assert calls == []  # save_picks_db (the legacy publication) never fired
    payload = picks_log_line(capsys.readouterr().out)
    assert payload["write_mode"] == "stat_update"
    assert payload["stat_column"]["stat_filled"] == 4
    assert payload["stat_column"]["legacy_wipe_replace"] is False
    assert len(payload["picks"]) == 3 and {row["tims"] for row in payload["picks"]} == {1, 2, 3}
    assert all(row["stat"] is not None for row in payload["picks"])


def test_stat_update_missing_slate_skips_cleanly_no_insert(env_vars, prediction_tables, monkeypatch, capsys):
    """TIMING dependency (the owner-corrected contract): when the run fires
    before the site's own SaveToDb publish, Picks-{ENV} has no rows for the
    date -> WARN + CLEAN skip (status 200, no exception, no write, no
    invented rows). The top-3 picks remain a log output of the frame."""
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    prediction_tables.picks_table.rows.clear()
    calls: list = []
    fake_save(monkeypatch, calls)

    result = mod.handler({"date": ON_DATE}, None)  # must NOT raise

    assert result["statusCode"] == 200
    assert result["stat_update"] == {"slate_rows": 0, "filled": 0, "no_frame": 0, "not_on_slate": 0, "skipped": True}
    assert calls == [] and prediction_tables.picks_table.patches == []
    out = capsys.readouterr().out
    assert "no rows for 2026-06-14" in out and "SKIPPED cleanly" in out
    assert "never inserting our own rows" in out
    payload = picks_log_line(out)
    assert payload["stat_column"]["stat_fill_skipped"] is True
    assert len(payload["picks"]) == 3  # the top-3 projection is still emitted


def test_stat_update_warns_slate_rows_without_a_prediction(env_vars, prediction_tables, monkeypatch, capsys):
    """(b) missing players: a slate row the model frame cannot score
    (call-up without history) is WARNed BY NAME and left UNTOUCHED - no
    invented stat, no inserted row."""
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    prediction_tables.picks_table.rows.append(
        {
            "id": 5,
            "date": ON_DATE,
            "player_id": 99,
            "name": "callup",
            "team_name": "Toronto",
            "stat": None,
            "tims": 1,
            "gpg": 0.0,
            "hgpg": 0.0,
        }
    )
    calls: list = []
    fake_save(monkeypatch, calls)

    result = mod.handler({"date": ON_DATE}, None)

    assert result["statusCode"] == 200
    assert result["stat_update"] == {"slate_rows": 5, "filled": 4, "no_frame": 1, "not_on_slate": 0, "skipped": False}
    out = capsys.readouterr().out
    assert "no model prediction (call-ups without published history" in out
    assert "'callup' (player_id 99)" in out  # WARNed by name
    untouched = [row for row in prediction_tables.picks_table.rows if row["id"] == 5][0]
    assert untouched["stat"] is None  # stat left UNTOUCHED
    assert len(prediction_tables.picks_table.rows) == 5  # no insert happened
    assert calls == []


def test_stat_update_warns_frame_players_absent_from_the_slate(env_vars, prediction_tables, monkeypatch, capsys):
    """(c) extra frame players: a scored player the slate does not carry
    (the site left him off - likely scratched) is WARNed, never written,
    and never inserted as a minimal row."""
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    picks = prediction_tables.picks_table
    picks.rows = [row for row in picks.rows if row["player_id"] != 2]  # the site dropped player 2
    calls: list = []
    fake_save(monkeypatch, calls)

    result = mod.handler({"date": ON_DATE}, None)

    assert result["statusCode"] == 200
    assert result["stat_update"] == {"slate_rows": 3, "filled": 3, "no_frame": 0, "not_on_slate": 1, "skipped": False}
    out = capsys.readouterr().out
    assert "not on today's Picks-prod slate" in out
    assert "no stat write for them" in out
    assert len(picks.rows) == 3 and [row["id"] for row in picks.rows] == [1, 3, 4]  # no row inserted
    assert {row_id for row_id, _pl, _p in picks.patches} == {1, 3, 4}
    assert calls == []


def test_stat_update_dry_is_read_only(env_vars, prediction_tables, monkeypatch, capsys):
    """(a/d) A dry pass in the DEFAULT mode PATCHes nothing; it still
    assembles + logs the top-3 output."""
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    picks = prediction_tables.picks_table
    before = [dict(row) for row in picks.rows]
    calls: list = []
    fake_save(monkeypatch, calls)

    result = mod.handler({"date": ON_DATE, "dry": True}, None)

    assert result["statusCode"] == 200 and result["dry"]
    assert "stat_update" not in result  # nothing computed toward a write
    assert picks.rows == before and picks.patches == []
    assert calls == []
    out = capsys.readouterr().out
    assert "dry: stat update skipped (read-only)" in out
    payload = picks_log_line(out)
    assert payload["stat_column"]["stat_fill_skipped"] is True


def test_invalid_write_mode_fails_closed(env_vars, prediction_tables, monkeypatch):
    """(d) The write-mode axis is whitelisted: a typo aborts the run BEFORE
    any read or write - the same fail-closed form resolve_env_axis has."""

    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    with pytest.raises(ValueError, match="invalid write mode"):
        mod.handler({"date": ON_DATE, "write_mode": "nope"}, None)


def test_legacy_write_mode_flag_still_wipes_and_publishes(env_vars, prediction_tables, monkeypatch, capsys):
    """(d) The legacy wipe+3 publication survives behind the explicit flag -
    exactly the pre-stat_update behavior (3 rows via save_picks_db)."""
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls: list = []
    fake_save(monkeypatch, calls)

    result = mod.handler({"date": ON_DATE, "write_mode": "legacy"}, None)

    assert result["statusCode"] == 200 and result["write_mode"] == "legacy"
    assert len(calls) == 3 and sorted(r["tims"] for r in calls) == [1, 2, 3]
    assert "stat_update" not in result
    payload = picks_log_line(capsys.readouterr().out)
    assert payload["write_mode"] == "legacy" and payload["stat_column"]["legacy_wipe_replace"] is True


def test_stat_update_top3_only_in_job_summary(env_vars, prediction_tables, monkeypatch, tmp_path):
    """(e) The job summary in the DEFAULT mode lists the top-3 picks table +
    the stat-fill headline; the picks themselves reach the summary ONLY as
    text output - the table mutation is stat-only on the site's rows."""
    summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    fake_s3(monkeypatch, make_bundle(today=datetime.date.today().isoformat()))
    calls: list = []
    fake_save(monkeypatch, calls)

    mod.handler({"date": ON_DATE}, None)

    text = summary.read_text(encoding="utf-8")
    assert "write_mode stat_update" in text
    assert "top-3 challenge picks (OUTPUT ONLY" in text
    assert "stat column updated on ALL players of the current Picks-{ENV} slate (no wipe" in text
    assert "stat fill: 4 of 4 slate row(s) patched" in text
    # (e) the top-3 rows appear ONLY as this summary table (the picks were
    # never written to the Picks table - save_picks_db never fired)
    table_rows = [line for line in text.splitlines() if re.match(r"^\| \d+ \| ", line)]
    assert len(table_rows) == 3
    assert any("| stat |" in line for line in text.splitlines())  # the table's header
    assert calls == []


def test_write_mode_cli_flag_maps_into_the_event(monkeypatch, capsys):
    """The CLI wiring: --write-mode rides the event; an unset flag adds NO
    key (the run() fallbacks then apply)."""
    events = []
    monkeypatch.setattr(mod, "run", lambda event, context=None: (events.append(event), {"statusCode": 200})[1])

    mod.main(["--date", "2026-10-23", "--write-mode", "legacy"])
    assert events[0]["write_mode"] == "legacy"

    mod.main(["--date", "2026-10-23"])
    assert "write_mode" not in events[1]  # default: no flag, no key

    with pytest.raises(SystemExit):
        mod.main(["--write-mode", "nope"])


def test_write_mode_resolution_order(monkeypatch):
    """The write-mode axis resolves CLI -> event -> WRITE_MODE var ->
    DEFAULT, and fails closed on anything else (the same form
    resolve_env_axis uses for the env axes)."""
    monkeypatch.delenv("WRITE_MODE", raising=False)
    assert mod.resolve_write_mode() == "stat_update"  # WRITE_MODE_DEFAULT
    monkeypatch.setenv("WRITE_MODE", "legacy")
    assert mod.resolve_write_mode() == "legacy"  # the env var beats the default
    assert mod.resolve_write_mode("stat_update") == "stat_update"  # the event beats the env var
    assert mod.resolve_write_mode("legacy", cli_value="stat_update") == "stat_update"  # CLI beats the event
    with pytest.raises(ValueError, match="invalid write mode"):
        mod.resolve_write_mode("nope")
