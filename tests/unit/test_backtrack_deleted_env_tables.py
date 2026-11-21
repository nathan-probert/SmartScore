"""Guards for the deleted dev backtrack tables (deleted_env_tables.py).

The guard is NAME-based: it raises on a resolved table name regardless of
whether that table still exists in Supabase (the empty shells were DROPped by
the owner Oct 2026), so none of these tests need a live connection.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, "smartscore/scripts/backtrack")

from deleted_env_tables import DELETED_BACKTRACK_TABLES, guard_backtrack_table

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


def test_dev_tables_named_in_error():
    for table in DELETED_BACKTRACK_TABLES:
        with pytest.raises(RuntimeError, match=table.replace("-", "\\-")):
            guard_backtrack_table(table)


def test_error_points_to_prod():
    for table in DELETED_BACKTRACK_TABLES:
        with pytest.raises(RuntimeError, match="ENV=prod"):
            guard_backtrack_table(table)


def test_deleted_registry_only_lists_verified_tables():
    assert DELETED_BACKTRACK_TABLES == {
        "Player-Snapshots-backtrack-dev",
        "Team-Stats-backtrack-dev",
        "Goalie-Snapshots-backtrack-dev",
    }


def test_prod_tables_pass_through():
    for prod in (
        "Player-Snapshots-backtrack-prod",
        "Team-Stats-backtrack-prod",
        "Goalie-Snapshots-backtrack-prod",
    ):
        assert guard_backtrack_table(prod) == prod


def test_guard_is_name_based_not_existence_based():
    """The guard must keep failing even after the shell disappears.

    The empty shells were DROPped by the owner Oct 2026 (PostgREST cannot
    DDL), so a PostgREST 404 is one possible failure mode - but the guard's
    promise is that the NAME itself is fatal before any request is made,
    whether or not the table still exists. Assert the raise is unconditional
    and carries the explicit prod pointer, with no Supabase client involved.
    """
    for table in sorted(DELETED_BACKTRACK_TABLES):
        with pytest.raises(RuntimeError) as excinfo:
            guard_backtrack_table(table)
        assert "ENV=prod" in str(excinfo.value)


def _migration_files():
    return sorted(MIGRATIONS.glob("*.sql"))


def test_migrations_never_scope_a_backtrack_table_via_env():
    """No migration may recreate the deleted -dev backtrack shells.

    The standing migrate job substitutes __ENV__ -> dev on PR deploys, so a
    `CREATE TABLE IF NOT EXISTS "<Name>-backtrack-__ENV__"` resurrects the
    deleted dev shell on the next main-push/PR run. All backtrack migrations
    are prod-pinned literally (BACKTRACK-PROD-PINNED headers; the CI guard in
    .github/workflows/deploy.yml enforces the same rule). Mirror that rule
    here so a regression fails `make test` too.
    """
    offenders = []
    for path in _migration_files():
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if "backtrack" in line.lower() and "__ENV__" in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, f"backtrack tables may not be __ENV__-scoped (dev backtrack tables are deleted): {offenders}"


def test_migrations_never_name_a_deleted_dev_table():
    """No migration file may spell one of the deleted table names at all."""
    for path in _migration_files():
        text = path.read_text()
        for table in DELETED_BACKTRACK_TABLES:
            assert table not in text, f"{path.name} still names deleted {table}"


def test_backtrack_migrations_are_retired():
    """The backtrack migration files are GONE from supabase/migrations/ -
    the Storage cutover dropped the family and its DDL with it.

    The standing migrate job re-applies every file in the directory, so a
    surviving backtrack migration would RESURRECT the dropped tables. The
    BACKTRACK-PROD-PINNED marker rule (deploy.yml 'Guard migration files')
    is now vacuous: no file may name a backtrack table at all."""
    backtrack_files = {p.name for p in _migration_files() if "backtrack" in p.stem}
    assert not backtrack_files, f"backtrack migrations must be retired at cutover: {sorted(backtrack_files)}"
    marker_files = {p.name for p in _migration_files() if "BACKTRACK-PROD-PINNED" in p.read_text()}
    assert not marker_files, f"prod-pin markers have nothing left to pin: {sorted(marker_files)}"
