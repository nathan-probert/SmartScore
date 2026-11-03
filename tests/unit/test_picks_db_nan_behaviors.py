"""Unit tests for the stat-only Picks write (run_daily_picks.stat_update_picks_db).

No AWS, no Supabase: the Picks table is an in-memory fake shaped like the
supabase-py builder chain, with an explicit column set (a live Picks table
carries exactly id/date/player_id/name/team_name/stat/tims/gpg/hgpg - it has
NO `ppg`, which a stat-only PATCH can never touch anyway; the whitelist
comment lives in run_daily_picks.REMOVE_KEYS). Asserted:

(a) the SMARTSCORE_ML_PICKS output rows never carry REMOVE_KEYS (`ppg`);
(b) the stat fill is a per-row, id-scoped PATCH that moves ONLY the stat
    column - NO delete, no insert, no upsert, row count/ids unchanged - on a
    3-row dev slate AND on a mocked full 495-row Picks-prod slate
    (the live-verified full-slate shape, mocked);
(c) a non-finite prediction PATCHes stat=None; a missing slate is a clean
    zero-write skip; call-ups are WARNed; a failing PATCH is LOUD and an
    all-patches-failed run raises.

The site's own pipeline keeps utility.save_to_db (its OWN full-slate wipe +
reinsert path, untouched); the ML path writes through stat_update only, so
the moment run_daily_picks' legacy wipe-then-3-row mode was removed, the
probe/wipe/restore scaffolding (save_picks_db) went with it - these tests
no longer cover it (git history retains the deleted tests).
Import bootstrap mirrors tests/unit/test_predict_picks.py.
"""

import copy
import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[0:0] = [str(ROOT / "smartscore"), str(ROOT / "smartscore" / "scripts" / "backtrack")]

# constants.py expects the compiled prediction wheel at import time (see
# test_predict_picks.py's note); the run path here only reads constant strings.
_rust_stub = types.ModuleType("make_predictions_rust")


class _StubWeights:  # noqa: N801
    def __init__(self, **_kwargs):
        self.__dict__.update(_kwargs)


_rust_stub.Weights = _StubWeights
sys.modules.setdefault("make_predictions_rust", _rust_stub)

_SPEC = importlib.util.spec_from_file_location(
    "run_daily_picks", ROOT / "smartscore" / "scripts" / "backtrack" / "run_daily_picks.py"
)
# Reuse the module tests/unit/test_predict_picks.py already loaded under the
# canonical "run_daily_picks" name when this file shares a session with it:
# BOTH files patch by string key ("run_daily_picks.SUPABASE_ADMIN_CLIENT"),
# and re-executing the module under the same sys.modules key would replace
# the entry with a new object - the other file's patches (and `mod`) would
# then point at a stale object -> cross-file pollution (26 failures). One
# shared module object per session, each test wiring its own fakes via
# monkeypatch (which restores attributes between tests), keeps both files'
# `mod` references consistent.
mod = sys.modules.get("run_daily_picks")
if not hasattr(mod, "__file__") or Path(mod.__file__).resolve() != Path(_SPEC.origin).resolve():
    mod = importlib.util.module_from_spec(_SPEC)
    sys.modules["run_daily_picks"] = mod
    _SPEC.loader.exec_module(mod)

ON_DATE = "2026-10-19"

# The live Picks-{ENV} column set (probed 2026-10-27: no `ppg` on either env;
# every remaining whitelisted key exists).
PICKS_COLUMNS = {"id", "date", "player_id", "name", "team_name", "stat", "tims", "gpg", "hgpg"}

PRIOR_ROWS = [
    {
        "id": 1,
        "date": "2026-10-11",
        "player_id": 55,
        "name": "old pick",
        "team_name": "T",
        "stat": 12.3,
        "tims": 1,
        "gpg": 0.3,
        "hgpg": 0.1,
    },
    {
        "id": 2,
        "date": "2026-10-11",
        "player_id": 56,
        "name": "old pick 2",
        "team_name": "T",
        "stat": 9.9,
        "tims": 2,
        "gpg": 0.2,
        "hgpg": 0.2,
    },
]


class FakePostgrestError(Exception):
    """A 4xx PostgREST error (PGRST204 = unknown column in a write)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class FakeResult:
    def __init__(self, rows):
        self.data = rows
        self.count = None


class FakeQueryBuilder:
    """Mini supabase-py builder: select/delete/upsert/eq/neq/limit/execute.

    Column-faithful: any request naming a column the table lacks raises the
    PGRST204-equivalent, exactly like the live Picks-{ENV} tables (that is
    what turns one unsupported key into wipe-then-fail). Every filter method
    chains, so the builder satisfies the `.delete().neq(...)` shape too.
    """

    def __init__(self, fake, table):
        self._fake = fake
        self._table = table
        self._mode = "select"
        self._columns = None
        self._payload = None
        self._eq = None

    def select(self, *columns, **_kw):
        self._mode = "select"
        joined = ",".join(columns)
        self._columns = [col.strip() for col in joined.split(",") if col.strip()]
        return self

    def update(self, payload):
        """The stat_update PATCH shape: update({...}).eq(col, val).execute()."""
        self._mode = "update"
        self._payload = dict(payload)
        return self

    def upsert(self, payload):
        self._mode = "upsert"
        self._payload = payload
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def limit(self, _n):
        return self

    def execute(self):
        fake = self._fake
        rows = fake.tables[self._table]
        if self._mode == "select":
            return self._execute_select(fake, rows)
        if self._mode == "update":
            return self._execute_update(fake, rows)
        raise AssertionError(f"unexpected builder mode {self._mode}")

    def _execute_select(self, fake, rows):
        if self._columns:
            fake.log.append(("select", tuple(self._columns)))
        if self._columns == ["*"]:  # PostgREST select=* means all columns
            return FakeResult([dict(row) for row in rows])
        unknown = [col for col in self._columns or [] if col not in fake.columns]
        if unknown:
            raise FakePostgrestError("PGRST204", f"column {self._table}.{unknown[0]} does not exist")
        return FakeResult([dict(row) for row in rows])

    def _execute_update(self, fake, rows):
        if fake.fail_updates:  # scripted PATCH failure(s), consumed in call order
            raise fake.fail_updates.pop(0)
        unknown = set(self._payload) - fake.columns
        if unknown:
            raise FakePostgrestError("PGRST204", f"column {self._table}.{sorted(unknown)[0]} does not exist")
        col, val = self._eq or (None, None)
        if col is None:
            raise AssertionError("unfiltered update (a stat fill must never be unscoped)")
        matched = [row for row in rows if row.get(col) == val]
        for row in matched:
            row.update(self._payload)
        fake.log.append(("update", (col, val, dict(self._payload), len(matched))))
        return FakeResult([dict(row) for row in rows])


class FakeSupabase:
    """One-table client with a fixed column set + an order-revealing call log."""

    def __init__(self, table, columns, rows=None, fail_updates=None):
        self.columns = set(columns)
        self.tables = {table: [dict(r) for r in (rows or [])]}
        self.log = []
        self.fail_updates = list(fail_updates or [])

    def table(self, name):
        assert len(self.tables) == 1, f"client wired for one table, asked for {name}"
        return FakeQueryBuilder(self, name)


def make_picks():
    """Three whitelisted output rows (ppg dropped by pick_rows_to_picks_db -
    the run's top-3 log/summary projection)."""
    picks = pd.DataFrame(
        {
            "date": [ON_DATE] * 3,
            "player_id": [101.0, 102.0, 103.0],
            "name": ["mack", "kadri", "raddysh"],
            "team_name": ["Colorado", "Colorado", "Toronto"],
            "tims": [1.0, 2.0, 3.0],
            "gpg": [0.5, 0.4, 0.3],
            "hgpg": [0.2, 0.21, 0.1],
            "ppg": [0.9, 0.8, 0.7],
            "ensemble": [0.999, 0.978, 1.0],
        }
    )
    return mod.pick_rows_to_picks_db(picks)


def wire_env(monkeypatch, fake):
    monkeypatch.setattr(mod, "ENV", "dev")
    monkeypatch.setattr(mod, "SUPABASE_ADMIN_CLIENT", fake)


def rows_of(fake, table="Picks-dev"):
    return fake.tables[table]


# ------------------------------------------------------------- (a) ppg dropped


def test_payload_never_carries_remove_keys():
    """The SMARTSCORE_ML_PICKS output rows are exactly the live table's
    column keys - `ppg` is gone."""
    rows = make_picks()
    assert len(rows) == 3
    for row in rows:
        assert set(row) == {"date", "player_id", "name", "team_name", "stat", "tims", "gpg", "hgpg"}
        assert set(row).isdisjoint(mod.REMOVE_KEYS)
    # the whitelist keeps the training-frame keys for the record; REMOVE_KEYS
    # decides what is serialized
    assert "ppg" in mod.PICK_DB_COLUMNS and mod.REMOVE_KEYS == ["ppg"]


# ------------------------------- stat_update (the ONLY write: stat-only PATCH)


def make_sim():
    """The scored sim, keyed to PRIOR_ROWS' player_ids (55/56) plus player
    99 - which the slate does NOT carry (the scratched-frame case)."""
    return pd.DataFrame(
        {
            "date": [ON_DATE] * 3,
            "player_id": [55, 56, 99],
            "tims": [1, 2, 1],
            "ensemble": [0.999, 0.111, 0.5],
        }
    )


def test_stat_value_convention_matches_the_legacy_percent():
    """stat_value is the DISPLAY convention (0-100 percent, 1 decimal);
    the DB write divides it by 100 at the stat_update PATCH boundary, so
    the column holds a 0-1 fraction. None on a non-finite prediction (a
    NaN must never patch a number into the column)."""
    assert mod.stat_value(0.12345) == 12.3
    assert mod.stat_value(0.999) == 99.9
    assert mod.stat_value(np.nan) is None
    assert mod.stat_value(float("inf")) is None


def test_stat_update_patches_only_stat_and_never_inserts(monkeypatch):
    """THE no-wipe contract at the write surface: stat_update issues ONLY
    per-row PATCHes (update().eq('id', ...)) - no delete, no insert, no
    upsert - and each PATCH carries ONLY the stat key. The other columns
    and the row count are untouched."""
    prior = copy.deepcopy(PRIOR_ROWS)
    fake = FakeSupabase("Picks-dev", PICKS_COLUMNS, rows=prior)
    wire_env(monkeypatch, fake)

    result = mod.stat_update_picks_db(make_sim(), ON_DATE)

    kinds = [entry[0] for entry in fake.log]
    assert kinds == ["select", "update", "update"]  # NO delete, NO insert, NO upsert
    for entry in fake.log:
        if entry[0] == "update":
            _col, row_id, payload, matched = entry[1]
            assert row_id in (1, 2)  # the PATCH is id-scoped
            assert set(payload) == {"stat"}  # ONLY the stat column moves
            assert matched == 1
    table = rows_of(fake)
    assert len(table) == 2  # row count unchanged (99 was never inserted)
    assert sorted(row["id"] for row in table) == [1, 2]  # ids untouched
    by_id = {row["id"]: row for row in table}
    assert by_id[1] == {**prior[0], "stat": 99.9 / 100}  # 0.999 - the 0-1 decimal write form
    assert by_id[2] == {**prior[1], "stat": 11.1 / 100}  # 0.111
    assert result["filled"] == 2 and result["slate_rows"] == 2 and result["not_on_slate"] == 1


def test_stat_update_nan_prediction_patches_none(monkeypatch):
    """A non-finite ensemble PATCHes stat=None (the column stays null) - it
    never crashes the fill nor lies with a number. Same convention as
    pick_rows_to_picks_db's stat cell (see mod.stat_value)."""
    sim = pd.DataFrame({"player_id": [55], "ensemble": [np.nan]})
    fake = FakeSupabase("Picks-dev", PICKS_COLUMNS, rows=PRIOR_ROWS)
    wire_env(monkeypatch, fake)

    result = mod.stat_update_picks_db(sim, ON_DATE)

    assert result["filled"] == 1
    assert rows_of(fake)[0]["stat"] is None


def test_stat_update_100th_percentile_writes_exactly_one(monkeypatch):
    """Micro-assert of the 0-1 decimal write boundary: a player at the
    100th percentile (stat_value -> 100) PATCHes stat == 1.0 EXACTLY."""
    sim = pd.DataFrame({"player_id": [55], "ensemble": [1.0]})
    fake = FakeSupabase("Picks-dev", PICKS_COLUMNS, rows=copy.deepcopy(PRIOR_ROWS))
    wire_env(monkeypatch, fake)

    result = mod.stat_update_picks_db(sim, ON_DATE)

    assert result["filled"] == 1
    assert rows_of(fake)[0]["stat"] == 1.0


def test_stat_update_missing_slate_is_a_clean_skip_with_zero_writes(monkeypatch):
    """The TIMING dependency: when the site's own SaveToDb publish has not
    written the date's slate yet, the mode returns skipped cleanly - exactly
    ONE read, no PATCH, no insert (the site's pipeline owns the rows)."""
    fake = FakeSupabase("Picks-dev", PICKS_COLUMNS, rows=[])
    wire_env(monkeypatch, fake)

    result = mod.stat_update_picks_db(make_sim(), ON_DATE)

    assert result == {"slate_rows": 0, "filled": 0, "no_frame": 0, "not_on_slate": 0, "skipped": True}
    assert [entry[0] for entry in fake.log] == ["select"]


def test_stat_update_warns_callups_and_leaves_their_stat(monkeypatch, capsys):
    """A slate row without a prediction (call-up without history) is WARNed
    by name and its stat stays untouched - never an invented value."""
    empty_stat_row = {**PRIOR_ROWS[0], "id": 3, "name": "callup", "player_id": 404, "stat": None}
    fake = FakeSupabase("Picks-dev", PICKS_COLUMNS, rows=PRIOR_ROWS + [empty_stat_row])
    wire_env(monkeypatch, fake)

    result = mod.stat_update_picks_db(make_sim(), ON_DATE)

    assert result["no_frame"] == 1 and result["filled"] == 2
    out = capsys.readouterr().out
    assert "callup' (player_id 404)" in out
    assert "stat left UNTOUCHED" in out
    assert rows_of(fake)[2]["stat"] is None


def test_stat_update_write_failure_is_loud_and_continues(monkeypatch, capsys):
    """One failing PATCH cannot withhold the fill: the failing row keeps the
    site's stat, the rest land, and the failure is LOUD (the same spirit as
    save_picks_db, minus the scaffolding a PATCH never needs)."""
    sim = pd.DataFrame({"player_id": [55, 56], "ensemble": [0.5, 0.6]})
    fake = FakeSupabase(
        "Picks-dev", PICKS_COLUMNS, rows=PRIOR_ROWS, fail_updates=[FakePostgrestError("PGRST204", "boom")]
    )
    wire_env(monkeypatch, fake)

    result = mod.stat_update_picks_db(sim, ON_DATE)

    assert result["filled"] == 1
    out = capsys.readouterr().out
    assert "stat PATCH failed for Picks-dev row id=1" in out
    assert "the next kick re-fills" in out


def test_stat_update_total_write_failure_raises(monkeypatch):
    """Every PATCH failing = the write path failed outright: raise (never a
    green run that wrote zero stat columns while the slate stood full)."""
    sim = pd.DataFrame({"player_id": [55, 56], "ensemble": [0.5, 0.6]})
    fake = FakeSupabase(
        "Picks-dev",
        PICKS_COLUMNS,
        rows=PRIOR_ROWS,
        fail_updates=[FakePostgrestError("PGRST204", "boom"), FakePostgrestError("PGRST204", "boom")],
    )
    wire_env(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="wrote zero stat columns"):
        mod.stat_update_picks_db(sim, ON_DATE)


def test_stat_update_full_prod_like_slate_495_rows(monkeypatch):
    """The 495-row Picks-prod full-slate shape, mocked (the live-verified
    prod run's setup): every slate row gets exactly ONE id-scoped PATCH
    carrying ONLY {"stat"}; row count, ids and every other column are
    unchanged; NO delete/insert/upsert happens anywhere."""
    slate = [
        {
            "id": row_id,
            "date": ON_DATE,
            "player_id": 1_000_000 + row_id,
            "name": f"player-{row_id}",
            "team_name": "T",
            "stat": 0.2 + (row_id % 10) * 0.01,  # the site's raw fractions
            "tims": (row_id % 3) + 1,
        }
        for row_id in range(1, 496)
    ]
    sim = pd.DataFrame(
        {
            "date": [ON_DATE] * 495,
            "player_id": [1_000_000 + row_id for row_id in range(1, 496)],
            "tims": [(row_id % 3) + 1 for row_id in range(1, 496)],
            "ensemble": [0.5 + (row_id % 50) / 100 for row_id in range(1, 496)],
        }
    )
    fake = FakeSupabase("Picks-prod", PICKS_COLUMNS, rows=slate)
    monkeypatch.setattr(mod, "ENV", "prod")
    monkeypatch.setattr(mod, "SUPABASE_ADMIN_CLIENT", fake)

    result = mod.stat_update_picks_db(sim, ON_DATE)

    assert result == {"slate_rows": 495, "filled": 495, "no_frame": 0, "not_on_slate": 0, "skipped": False}
    kinds = [entry[0] for entry in fake.log]
    assert kinds == ["select"] + ["update"] * 495  # one read + 495 PATCHes: NO delete / no insert / no upsert
    assert len({(e[1][0], e[1][1]) for e in fake.log if e[0] == "update"}) == 495  # every id unique
    before = {r["id"]: dict(r) for r in slate}
    after = {r["id"]: dict(r) for r in fake.tables["Picks-prod"]}
    assert set(after) == set(before) and len(after) == 495  # ids/count unchanged
    for row_id, old in before.items():
        assert old["stat"] != after[row_id]["stat"]  # the stat column moved...
        old.pop("stat"), after[row_id].pop("stat")
        assert old == after[row_id]  # ...and ONLY it did
