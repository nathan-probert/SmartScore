#!/usr/bin/env python
"""The full ML history ledger as per-season compressed files in Supabase Storage.

WHY THIS EXISTS
---------------
The published backtrack family (~560 MB JSON-encoded) sits in normal
Supabase tables and blows the free plan's 500 MB database cap, while the
free Storage plan offers 1 GB with a 50 MB per-file upload ceiling. So the
ledger lives in the bucket as ONE SQLITE FILE PER SEASON, gzip-compressed:
~45k rows/season lands each file at roughly 5-15 MB - comfortably under the
50 MB cap - and the split composes perfectly with how the data ages:

* historical season files are WRITE-ONCE: a season's rows only change if
  someone backfills history, which the offline top-up tools own;
* the nightly job (ml-picks.yml's `nightly` job) downloads every file,
  materializes them into one working database (the career-long features -
  ewm_*/career_* - span all seasons), backfills yesterday's games into it,
  computes the next slate's feature rows, publishes them to the small
  ML-Daily-Features-{ENV} table, and re-uploads ONLY the season files whose
  bytes changed (gzip is deterministic, so an unchanged historical season
  re-compresses to the identical file and is skipped by size compare);
* the ~15 daily kicks never touch the ledger - they read the live snapshot
  and the small features table and stay read-only (see
  docs/ml-ledger-runbook.md).

Season assignment: the same month rule run_daily_picks.season_label
documents, COVID bubble exceptions included - rows are split by their
published semantics, not by calendar guesswork.

Local training (train_export.py, the owner's machine) reads the SAME
files: `python ml_ledger.py pull` materializes the working database; there
is no local-only copy of the truth anywhere.

Usage::

    uv run python smartscore/scripts/backtrack/ml_ledger.py pull  # bucket -> data/ledger/working.sqlite
    uv run python smartscore/scripts/backtrack/ml_ledger.py push  # working.sqlite -> bucket (changed files only)
    uv run python smartscore/scripts/backtrack/ml_ledger.py verify  # counts per season file vs the working db
"""

import argparse
import datetime
import gzip
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

_here = Path(__file__).resolve()
_root = _here.parents[3]
sys.path.insert(0, str(_root / "smartscore"))
sys.path.insert(1, str(_here.parent))

import ml_history_store  # noqa: E402  (same directory: the ledger's SQLite layer)

from config import SUPABASE_ADMIN_CLIENT  # noqa: E402

LEDGER_PREFIX = os.environ.get("ML_LEDGER_PREFIX", "ml/ledger").strip("/")

# Split/materialize helpers, imported off the store after the path juggling
# above (E402 is the house pattern for these scripts).
SCHEMA = ml_history_store.SCHEMA
TABLE_MAP = ml_history_store.TABLE_MAP
PRIMARY_KEYS = ml_history_store.PRIMARY_KEYS
_insert_sql = ml_history_store._insert_sql

LEDGER_PREFIX = os.environ.get("ML_LEDGER_PREFIX", "ml/ledger").strip("/")
LEDGER_BUCKET = os.environ.get("ML_STORAGE_BUCKET", "ml-artifacts")
LEDGER_WORK_DIR = _root / "data" / "ledger"

# The four tables the ledger carries, in copy order (markers last so a
# re-run's idempotent upserts land after the rows they describe).
LEDGER_TABLES = list(TABLE_MAP)


def season_of(date_str):
    """The NHL season id ('20242025') a date belongs to - run_daily_picks.
    season_label's month rule with the two COVID-era exceptions (the 2019-20
    bubble post-season keyed 20192020, the delayed 2020-21 post-season keyed
    20202021). Defined HERE so the ledger split and the run share one
    implementation; run_daily_picks.season_label delegates to it."""
    day = datetime.date.fromisoformat(str(date_str))
    if datetime.date(2020, 7, 1) <= day <= datetime.date(2020, 9, 30):
        return "20192020"  # the COVID bubble: the 2019-20 season's playoffs
    if datetime.date(2021, 7, 1) <= day <= datetime.date(2021, 7, 31):
        return "20202021"  # the delayed 2020-21 post-season
    start = day.year if day.month >= 7 else day.year - 1
    return f"{start}{start + 1}"


# ------------------------------------------------------------- split / join


def split_seasons(db_path, out_dir):
    """One SQLite file per season under ``out_dir``; returns {season: path}.

    Every table splits by season_of(date), so the four tables agree on
    which file a row belongs to (team_stats/goalie carry a season column,
    the player table keys by date alone - season_of is the shared truth)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        src.row_factory = sqlite3.Row
        for table in LEDGER_TABLES:
            rows = pd.read_sql_query(f'SELECT * FROM "{TABLE_MAP[table]}"', src)
            rows["date"] = rows["date"].astype(str)
            rows["_season"] = rows["date"].map(season_of)
            for season, season_group in rows.groupby("_season"):
                season_rows = season_group.drop(columns="_season")
                out_path = out_dir / f"{season}.sqlite"
                written = result.get(season)
                if written is None:
                    written = _fresh_db(out_path)
                    result[season] = written
                _write_rows(written, TABLE_MAP[table], season_rows)
    finally:
        src.close()
    return result


def _fresh_db(out_path):
    """A new per-season file with the shared schema. Overwrites silently -
    the split always derives from the working database, never accumulates."""
    if Path(out_path).exists():
        Path(out_path).unlink()
    con = sqlite3.connect(out_path)
    try:
        con.executescript(SCHEMA)
        con.execute("PRAGMA journal_mode=DELETE")  # no -wal sidecars to ship
        con.commit()
    finally:
        con.close()
    return out_path


def _write_rows(out_path, local_table, frame):
    """Append one table's rows (already filtered to one season) to a
    per-season file."""
    con = sqlite3.connect(out_path)
    try:
        records = frame.to_dict("records")
        pk = PRIMARY_KEYS[local_table]
        shapes: dict[tuple, list[dict]] = {}
        for row in records:
            shapes.setdefault(tuple(sorted(row)), []).append(row)
        for shape_rows in shapes.values():
            cols = list(shape_rows[0])
            update_cols = [c for c in cols if c not in pk]
            sql = _insert_sql(local_table, cols, update_cols)
            con.executemany(sql, [tuple(row[c] for c in cols) for row in shape_rows])
        con.commit()
    finally:
        con.close()


def materialize(season_files, out_path):
    """Merge per-season files into ONE working database (the career-long
    features span seasons - the working db is what backfill and the feature
    publisher read). ATTACH + INSERT keeps this a pure file copy."""
    out_path = Path(out_path)
    if out_path.exists():
        out_path.unlink()
    con = sqlite3.connect(out_path)
    try:
        con.executescript(SCHEMA)
        for season, path in sorted(season_files.items()):
            con.execute("ATTACH DATABASE ? AS season_db", (str(path),))
            for table in LEDGER_TABLES:
                local = TABLE_MAP[table]
                con.execute(f'INSERT OR REPLACE INTO "{local}" SELECT * FROM season_db."{local}"')
            con.commit()  # ATTACH/DETACH cannot run inside a transaction
            con.execute("DETACH DATABASE season_db")
        con.commit()
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    return out_path


# ------------------------------------------------------------------ storage


def _storage():
    return SUPABASE_ADMIN_CLIENT.storage.from_(LEDGER_BUCKET)


def _key(season):
    return f"{LEDGER_PREFIX}/{season}.sqlite.gz"


def _gzipped(path):
    gz_path = Path(str(path) + ".gz")
    gz_path.write_bytes(gzip.compress(Path(path).read_bytes(), compresslevel=9))
    return gz_path


def upload_season(season, sqlite_path):
    """gzip + upload one season file. Returns the uploaded byte size."""
    gz = _gzipped(sqlite_path)
    payload = gz.read_bytes()
    _storage().upload(_key(season), payload, {"upsert": "true", "content-type": "application/gzip"})
    size = len(payload)
    gz.unlink(missing_ok=True)
    return size


def remote_sizes():
    """{season: byte size} from the bucket listing (the change detector:
    gzip is deterministic, so an unchanged season re-compresses to the
    identical byte count and the nightly push skips it)."""
    listing = _storage().list(LEDGER_PREFIX)
    sizes = {}
    for entry in listing:
        name = entry.get("name") or ""
        if name.endswith(".sqlite.gz"):
            sizes[name[: -len(".sqlite.gz")]] = (entry.get("metadata") or {}).get("size") or 0
    return sizes


def push(working_db=None):
    """Split the working database into seasons and upload the files whose
    bytes changed. Returns {season: ("uploaded"|"unchanged", size)}."""
    working_db = Path(working_db or default_working_db())
    out = {}
    sizes = remote_sizes()
    for season, path in split_seasons(working_db, LEDGER_WORK_DIR).items():
        gz = _gzipped(path)
        size = gz.stat().st_size
        gz.unlink(missing_ok=True)
        if sizes.get(season) == size:
            out[season] = ("unchanged", size)
            continue
        out[season] = ("uploaded", upload_season(season, path))
        print(f"  {season}: uploaded {size / 1e6:.1f} MB", flush=True)
    return out


def pull(work_dir=None):
    """Download every season file and materialize the working database.
    Returns the working db path."""
    work_dir = Path(work_dir or LEDGER_WORK_DIR)
    work_dir.mkdir(parents=True, exist_ok=True)
    listing = _storage().list(LEDGER_PREFIX)
    seasons = sorted(
        (entry.get("name") or "").removesuffix(".sqlite.gz")
        for entry in listing
        if str(entry.get("name", "")).endswith(".sqlite.gz")
    )
    if not seasons:
        raise RuntimeError(
            f"no ledger files under {LEDGER_BUCKET}/{LEDGER_PREFIX}/ - seed first: "
            "uv run python smartscore/scripts/backtrack/ml_ledger.py seed"
        )
    parts = {}
    for season in seasons:
        payload = _storage().download(_key(season))
        sqlite_path = work_dir / f"{season}.sqlite"
        sqlite_path.write_bytes(gzip.decompress(payload))
        parts[season] = sqlite_path
        print(f"  {season}: {(len(payload)) / 1e6:.1f} MB gz", flush=True)
    return materialize(parts, work_dir / "working.sqlite")


def seed(working_db=None):
    """ONE-TIME seed: copy the four Supabase tables into the bucket.

    Runs the idempotent bootstrap (ml_history_store.bootstrap, month-window
    paging) then pushes every season file. ~10 minutes and ~130 MB of
    PostgREST reads - a fraction of the 5 GB monthly egress."""
    working_db = Path(working_db or default_working_db())
    for table in LEDGER_TABLES:
        n = ml_history_store.bootstrap(table, delay=0.02)
        print(f"  bootstrapped {table}: {n:,} rows", flush=True)
    pushed = push(working_db)
    total = sum(size for _, (_, size) in pushed.items())
    print(f"seeded {len(pushed)} season file(s), {total / 1e6:.1f} MB total", flush=True)
    return pushed


def verify(working_db=None):
    """Row counts per table in the working database + the bucket's file
    inventory (the seed/push receipt)."""
    working_db = Path(working_db or default_working_db())
    sizes = remote_sizes()
    src = sqlite3.connect(f"file:{working_db}?mode=ro", uri=True)
    try:
        for table in LEDGER_TABLES:
            local_n = src.execute(f'SELECT COUNT(*) FROM "{TABLE_MAP[table]}"').fetchone()[0]
            print(f"{table}: working db {local_n:,} rows", flush=True)
    finally:
        src.close()
    total = sum(sizes.values())
    print(f"bucket: {len(sizes)} season file(s), {total / 1e6:.1f} MB total: {sizes}", flush=True)
    return True


def default_working_db():
    return LEDGER_WORK_DIR / "working.sqlite"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["seed", "pull", "push", "verify"], help="what to do")
    parser.add_argument("--db", help="working database path (default: data/ledger/working.sqlite)")
    args = parser.parse_args(argv)

    target = Path(args.db) if args.db else default_working_db()
    if args.command == "seed":
        seed(target)
    elif args.command == "pull":
        path = pull()
        print(f"materialized ledger -> {path}", flush=True)
    elif args.command == "push":
        push(target)
    else:
        verify(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
