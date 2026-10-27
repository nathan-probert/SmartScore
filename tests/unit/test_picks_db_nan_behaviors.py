"""Unit tests for the schema-safe Picks-{ENV} write (run_daily_picks.save_picks_db).

No AWS, no Supabase: the Picks table is an in-memory fake shaped like the
supabase-py builder chain, with an explicit column set (a live Picks table
carries exactly id/date/player_id/name/team_name/stat/tims/gpg/hgpg - it has
NO `ppg`, which is the smoke-run hazard these tests pin: the legacy
wipe-then-insert shipped `ppg`, the wipe succeeded, the insert failed
PGRST204, and Picks-dev was left at 0 rows while Picks-prod stayed 494).
Asserted:

(a) the payload sent to the Picks table NEVER contains REMOVE_KEYS (`ppg`);
(b) the wipe-then-insert convention (delete-neq then reinsert, ids from 1)
    lands exactly the run's rows, and a repeat run overwrites, not duplicates;
(c) an insert failure AFTER the wipe restores the captured pre-wipe rows and
    raises loud - the realized end state is the OLD table, never 0 (and a
    restore that itself fails reports both errors in one raise);
(d) an unknown payload key raises BEFORE any delete - the table is untouched.

The site's own pipeline keeps utility.save_to_db; the ML picks pass writes
through save_picks_db so a failed run cannot empty a table. Import bootstrap
mirrors tests/unit/test_predict_picks.py.
"""

import importlib.util
import sys
import types
from pathlib import Path

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

    def select(self, *columns, **_kw):
        self._mode = "select"
        joined = ",".join(columns)
        self._columns = [col.strip() for col in joined.split(",") if col.strip()]
        return self

    def delete(self):
        self._mode = "delete"
        return self

    def upsert(self, payload):
        self._mode = "upsert"
        self._payload = payload
        return self

    def eq(self, _col, _val):  # unused in these writes; chainability only
        return self

    def neq(self, _col, _val):
        return self

    def limit(self, _n):
        return self

    def execute(self):
        fake = self._fake
        rows = fake.tables[self._table]
        if self._mode == "select":
            if self._columns:
                fake.log.append(("select", tuple(self._columns)))
            if self._columns == ["*"]:  # PostgREST select=* means all columns
                return FakeResult([dict(row) for row in rows])
            unknown = [col for col in self._columns or [] if col not in fake.columns]
            if unknown:
                raise FakePostgrestError("PGRST204", f"column {self._table}.{unknown[0]} does not exist")
            return FakeResult([dict(row) for row in rows])
        if self._mode == "delete":
            fake.log.append(("delete", len(rows)))
            fake.tables[self._table] = []
            return FakeResult([])
        if self._mode == "upsert":
            if fake.fail_upserts:  # scripted failure(s), consumed in call order
                raise fake.fail_upserts.pop(0)
            unknown = {key for row in self._payload for key in row} - fake.columns
            if unknown:
                raise FakePostgrestError("PGRST204", f"column {self._table}.{sorted(unknown)[0]} does not exist")
            fake.log.append(("insert", len(self._payload)))
            by_id = {row.get("id"): dict(row) for row in rows}
            for row in self._payload:
                by_id[row.get("id")] = dict(row)
            fake.tables[self._table] = sorted(by_id.values(), key=lambda r: str(r.get("id")))
            return FakeResult(self._payload)
        raise AssertionError(f"unexpected builder mode {self._mode}")


class FakeSupabase:
    """One-table client with a fixed column set + an order-revealing call log."""

    def __init__(self, table, columns, rows=None, fail_upserts=None):
        self.columns = set(columns)
        self.tables = {table: [dict(r) for r in (rows or [])]}
        self.log = []
        self.fail_upserts = list(fail_upserts or [])

    def table(self, name):
        assert len(self.tables) == 1, f"client wired for one table, asked for {name}"
        return FakeQueryBuilder(self, name)


def make_picks():
    """Three whitelisted pick rows (ppg dropped by pick_rows_to_picks_db)."""
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
    """The Picks payload is exactly the live table's columns - `ppg` is gone."""
    rows = make_picks()
    assert len(rows) == 3
    for row in rows:
        assert set(row) == {"date", "player_id", "name", "team_name", "stat", "tims", "gpg", "hgpg"}
        assert set(row).isdisjoint(mod.REMOVE_KEYS)
    # the whitelist keeps the training-frame keys for the record; REMOVE_KEYS
    # decides what is serialized
    assert "ppg" in mod.PICK_DB_COLUMNS and mod.REMOVE_KEYS == ["ppg"]


# ------------------------------------------------- (b) wipe-then-insert happy


def test_wipe_then_insert_preserves_convention_and_ids(monkeypatch):
    fake = FakeSupabase("Picks-dev", PICKS_COLUMNS, rows=PRIOR_ROWS)
    wire_env(monkeypatch, fake)

    rows = make_picks()
    mod.save_picks_db(rows, ON_DATE)

    kinds = [entry[0] for entry in fake.log]
    # KEY ORDER assert: probe select + pre-wipe capture BEFORE the one delete,
    # the insert after the delete, and the post-write verify select last.
    assert kinds == ["select", "select", "delete", "insert", "select"]
    # the delete saw exactly the 2 pre-wipe rows
    assert fake.log[kinds.index("delete")] == ("delete", 2)
    assert [entry[1] for entry in fake.log if entry[0] == "insert"] == [3]

    table = rows_of(fake)
    assert len(table) == 3
    assert sorted(row["id"] for row in table) == [1, 2, 3]  # ids restarted at 1
    assert all(row["date"] == ON_DATE and "ppg" not in row for row in table)
    by_name = {row["name"]: row for row in table}
    assert by_name["kadri"]["stat"] == 97.8  # ensemble ranked-pct -> percent
    assert by_name["raddysh"]["tims"] == 3


def test_second_run_rewrites_the_same_rows_not_duplicates(monkeypatch):
    """The per-game-kick idempotency contract: same date rerun = overwrite."""
    fake = FakeSupabase("Picks-dev", PICKS_COLUMNS, rows=PRIOR_ROWS)
    wire_env(monkeypatch, fake)

    rows = make_picks()
    mod.save_picks_db(rows, ON_DATE)
    mod.save_picks_db(rows, ON_DATE)

    assert len(rows_of(fake)) == 3
    assert len([e for e in fake.log if e[0] == "delete"]) == 2
    assert sorted(r["name"] for r in rows_of(fake)) == ["kadri", "mack", "raddysh"]


# ------------------------------------------------ (c) insert failure + restore


def test_insert_failure_restores_prior_rows_and_raises(monkeypatch):
    """Insert 400s AFTER the wipe -> the OLD rows come back and the run raises."""
    fake = FakeSupabase(
        "Picks-dev",
        PICKS_COLUMNS,
        rows=PRIOR_ROWS,
        fail_upserts=[FakePostgrestError("PGRST204", "column Picks-dev.bogus does not exist")],
    )
    wire_env(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="insert FAILED.*OLD state.*original error"):
        mod.save_picks_db(make_picks(), ON_DATE)

    table = rows_of(fake)
    # realized end state = the OLD table, with its original ids, not 0
    assert len(table) == len(PRIOR_ROWS)
    assert sorted(row["name"] for row in table) == ["old pick", "old pick 2"]
    assert sorted(int(row["id"]) for row in table) == [1, 2]
    assert all(row["date"] == "2026-10-11" for row in table)
    # failed insert logs nothing (raise precedes the log); the restore logs one
    assert [(e[0], e[1]) for e in fake.log if e[0] == "insert"] == [("insert", 2)]
    assert any(e[0] == "delete" for e in fake.log)  # the wipe did happen


def test_restore_failure_reports_both_errors_loudly(monkeypatch):
    """Best-effort restore: when IT also 400s, ONE raise names both failures."""
    fake = FakeSupabase(
        "Picks-dev",
        PICKS_COLUMNS,
        rows=PRIOR_ROWS,
        fail_upserts=[
            FakePostgrestError("PGRST204", "insert 400"),
            FakePostgrestError("42506", "restore 400"),
        ],
    )
    wire_env(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="RESTORE ALSO failed.*WIPE-EMPTY.*original error.*restore error"):
        mod.save_picks_db(make_picks(), ON_DATE)
    # and the wipe still happened - no third upsert attempt
    assert any(e[0] == "delete" for e in fake.log)


# --------------------------------------------- (d) unknown column fails first


def test_unknown_column_raises_before_any_delete(monkeypatch):
    fake = FakeSupabase("Picks-dev", PICKS_COLUMNS, rows=PRIOR_ROWS)
    wire_env(monkeypatch, fake)

    rows = make_picks()
    rows[0]["five_gpg"] = 0.25  # a training-frame key no Picks table has

    with pytest.raises(RuntimeError, match="aborted BEFORE any delete"):
        mod.save_picks_db(rows, ON_DATE)

    kinds = [entry[0] for entry in fake.log]
    assert "delete" not in kinds and "insert" not in kinds
    assert rows_of(fake) == [dict(r) for r in PRIOR_ROWS]  # table untouched


def test_unknown_column_on_last_row_is_caught_by_the_probe(monkeypatch):
    """The probe ranges over EVERY pick row's keys, not just the first."""
    fake = FakeSupabase("Picks-dev", PICKS_COLUMNS, rows=PRIOR_ROWS)
    wire_env(monkeypatch, fake)

    rows = make_picks()
    rows[2]["hppg"] = 0.4

    with pytest.raises(RuntimeError, match="hppg"):
        mod.save_picks_db(rows, ON_DATE)
    assert [e for e in fake.log if e[0] == "delete"] == []


def test_empty_payload_writes_nothing(monkeypatch):
    fake = FakeSupabase("Picks-dev", PICKS_COLUMNS, rows=PRIOR_ROWS)
    wire_env(monkeypatch, fake)

    mod.save_picks_db([], ON_DATE)
    assert fake.log == []  # no probe, no wipe, no insert
    assert rows_of(fake) == [dict(r) for r in PRIOR_ROWS]
