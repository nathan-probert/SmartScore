#!/usr/bin/env python
"""Export the raw store to a gzipped SQL dump, one file per season.

WHY A DUMP RATHER THAN THE DATABASE
-----------------------------------
``raw_nhl.sqlite`` is the working copy and is gitignored. What is committed is
``data/dumps/<season>.sql.gz`` - the same data as gzipped SQL text. Three reasons:

* **Size.** The 32.6 MB database gzips to 4.1 MB; a text dump gzips to 1.7 MB.
* **Reviewable.** A dump is ``INSERT`` statements, so a bad change to the
  reconstruction shows up as a readable diff in the PR instead of an opaque blob
  swap. That is the whole point of keeping the raw layer versioned - it is the
  audit trail for every rate computed from it.
* **Portable.** ``sqlite3 db < dump.sql`` rebuilds it anywhere, with no SQLite
  version skew and no schema-migration step.

Per-season rather than one dump, so adding a season writes one new file instead of
rewriting a multi-megabyte blob, which keeps the history small.

Usage::

    uv run python smartscore/scripts/backtrack/dump_store.py --export
    uv run python smartscore/scripts/backtrack/dump_store.py --load
    uv run python smartscore/scripts/backtrack/dump_store.py --list
"""

import argparse
import gzip
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = REPO_ROOT / "data" / "raw_nhl.sqlite"
DUMP_DIR = REPO_ROOT / "data" / "dumps"

# Tables holding per-season derived output. Included in the dump rather than
# recomputed on load: they are cheap to regenerate but shipping them means a loaded
# database is immediately queryable instead of needing a --derive pass.
_ALL_TABLES = ("player_games", "derived_features", "derived_team_stats")


def seasons_in_db(db_path=DB_PATH):
    if not Path(db_path).exists():
        return []

    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT DISTINCT season FROM player_games ORDER BY season")]
    finally:
        conn.close()


def _iter_season_lines(conn, season):
    """Yield the SQL statements that recreate one season's rows.

    Hand-rolled rather than ``iterdump`` because iterdump emits the whole database
    including every other season - a per-season file has to filter, and a plain
    INSERT generator is both smaller and easier to read in a diff.
    """
    yield "BEGIN TRANSACTION;"

    for table in _ALL_TABLES:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()

        if not exists:
            continue

        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]  # noqa: S608

        # S608: `table` is one of the _ALL_TABLES constants above, never input;
        # `columns` come from PRAGMA table_info on that same table. The only
        # variable is the bound `season` parameter.
        select = f"SELECT {', '.join(columns)} FROM {table} WHERE season = ?"  # noqa: S608

        for row in conn.execute(select, (season,)):
            values = ", ".join(_literal(v) for v in row)
            yield f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({values});"  # noqa: S608

    yield "COMMIT;"


def _literal(value):
    """Render a Python value as a SQL literal (None renders as NULL)."""
    if value is None:
        return "NULL"

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        return repr(value)

    text = str(value)
    escaped = text.replace("'", "''")

    return f"'{escaped}'"


def export_season(season, db_path=DB_PATH, dump_dir=DUMP_DIR):
    """Write data/dumps/<season>.sql.gz. Returns the path, or None if no data."""
    conn = sqlite3.connect(db_path)

    try:
        row_count = conn.execute(
            "SELECT COUNT(*) FROM player_games WHERE season = ?",
            (season,),
        ).fetchone()[0]

        if not row_count:
            print(f"{season}: no rows, skipping")
            return None

        dump_path = Path(dump_dir) / f"{season}.sql.gz"
        dump_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to a temp file and rename, so an interrupted export cannot leave a
        # truncated .gz that reads as a valid but incomplete season.
        tmp = dump_path.with_suffix(".gz.tmp")

        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as f:
            for line in _iter_season_lines(conn, season):
                f.write(line + "\n")

        tmp.replace(dump_path)
    finally:
        conn.close()

    size_mb = dump_path.stat().st_size / 1e6
    print(f"{season}: {row_count} row(s) -> {dump_path.name} ({size_mb:.1f} MB)")

    return dump_path


def export_all(db_path=DB_PATH, dump_dir=DUMP_DIR):
    seasons = seasons_in_db(db_path)

    if not seasons:
        print(f"no seasons in {db_path}")
        return []

    return [p for p in (export_season(s, db_path, dump_dir) for s in seasons) if p]


def load(db_path=DB_PATH, dump_dir=DUMP_DIR, clear=True):
    """Rebuild the working database from every dump in dump_dir."""
    import local_store  # noqa: PLC0415 - local import so --list works without it

    dump_paths = sorted(Path(dump_dir).glob("*.sql.gz"))

    if not dump_paths:
        print(f"no dumps in {dump_dir}; nothing to load")
        return 0

    conn = local_store.connect(Path(db_path))

    if clear:
        # Truncate, not drop: schema and indexes come from SCHEMA and are shared
        # across seasons, so a load replaces rows rather than recreating tables.
        for table in _ALL_TABLES:
            conn.execute(f"DELETE FROM {table}")  # noqa: S608
        conn.commit()

    for dump_path in dump_paths:
        with gzip.open(dump_path, "rt", encoding="utf-8") as f:
            conn.executescript(f.read())

        season = dump_path.name.removesuffix(".sql.gz")
        rows = conn.execute(
            "SELECT COUNT(*) FROM player_games WHERE season = ?",
            (season,),
        ).fetchone()[0]
        print(f"loaded {dump_path.name}: {rows} row(s)")

    total = conn.execute("SELECT COUNT(*) FROM player_games").fetchone()[0]
    conn.close()
    print(f"total: {total} row(s) in {db_path}")

    return total


def list_dumps(dump_dir=DUMP_DIR):
    dump_paths = sorted(Path(dump_dir).glob("*.sql.gz"))

    if not dump_paths:
        print(f"no dumps in {dump_dir}")
        return

    total = 0
    for p in dump_paths:
        size_mb = p.stat().st_size / 1e6
        total += size_mb
        print(f"  {p.name:<24} {size_mb:6.1f} MB")

    print(f"  {'total':<24} {total:6.1f} MB")


def main():
    parser = argparse.ArgumentParser(description="Export/import the raw store as gzipped SQL dumps.")
    parser.add_argument("--export", action="store_true", help="Write a dump per season.")
    parser.add_argument("--season", help="Export only this season.")
    parser.add_argument("--load", action="store_true", help="Rebuild the database from dumps.")
    parser.add_argument("--list", action="store_true", help="List existing dumps.")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--dumps", default=str(DUMP_DIR))
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))

    if args.list:
        list_dumps(args.dumps)
        return 0

    if args.export:
        if args.season:
            export_season(args.season, args.db, args.dumps)
        else:
            export_all(args.db, args.dumps)
        return 0

    if args.load:
        load(args.db, args.dumps)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
