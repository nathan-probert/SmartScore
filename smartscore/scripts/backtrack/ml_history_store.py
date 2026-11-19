#!/usr/bin/env python
"""Local SQLite store for the ML backtrack family - the Supabase decoupling.

WHY THIS EXISTS
---------------
The published backtrack family (Player-Snapshots-backtrack-prod ~624k rows,
Team-Stats-backtrack-prod, Goalie-Snapshots-backtrack-prod,
Player-Game-Raw-Markers-prod) is ~560 MB JSON-encoded - over the Supabase
free tier's 500 MB database cap - and the daily picks run reads the whole
ledger through PostgREST on every pass (~15 runs per slate date), so DB
size AND egress both scale with it.

The data was only in Supabase because the daily picks run executed on
GitHub Actions ephemeral runners, which cannot reach this machine's SQLite.
The raw layer has ALWAYS lived locally (data/raw_nhl.sqlite - the migrations
name it the source of truth); the Supabase tables are derived copies. With
the daily run moving onto this machine (see docs/ml-picks-local-runbook.md),
the derived tables can live next to the raw store and Supabase keeps only
what the website touches (Picks, Historic-Picks, Working, Metrics,
Player-Snapshots-{env}, user_preferences, auth).

THE BACKEND SWITCH
------------------
``ML_HISTORY_BACKEND`` selects where history reads/writes go:

* ``supabase`` (DEFAULT) - unchanged behavior. CI, the unit tests, and the
  GitHub Actions daily run keep reading/writing the Supabase tables, so
  merging this code changes nothing until the local scheduler takes over.
* ``sqlite`` - history reads/writes hit ``data/ml_history.sqlite`` (override
  the path with ``ML_HISTORY_DB``). The live snapshot read
  (run_daily_picks.today_live_rows -> Player-Snapshots-{env}) and the Picks /
  Historic-Picks writes stay on Supabase in BOTH modes - they belong to the
  website's tables, not this family.

DUAL-WRITE (the parity window)
------------------------------
``ML_HISTORY_DUAL_WRITE=1`` (default off) makes the SQLite write path ALSO
mirror every upsert to the Supabase tables. The mirror failure is LOUD but
never fatal - SQLite is the source of truth once the run is local; the
mirror exists only to keep the Supabase copies current so the GitHub
Actions path keeps working unchanged while the local run is being
validated side by side (docs/ml-picks-local-runbook.md, "parity week").
Flip it off at cutover.

SCHEMA
------
The DDL below mirrors supabase/migrations verbatim (20261008 / 20261011 /
20261015 / 20261024 / 20261027), same names, same PKs, same NULL semantics.
The one deliberate dtype difference: SQLite has no BOOLEAN, so ``home`` /
``opp_goalie_confirmed`` store as INTEGER 0/1 where PostgREST returned
JSON true/false. Downstream pandas truthiness is identical (``1 == True``),
and the parity diff compares values, not dtypes.

The SQLite upsert is a single ON CONFLICT DO UPDATE that sets ONLY the
columns present in the row - unmentioned columns keep their stored value.
That is exactly the semantics the two-lane (update/create) split forces on
PostgREST (a payload key absent from a PostgREST upsert would be an
explicit NULL), so the caller's lane split stays correct without change.

Usage::

    uv run python smartscore/scripts/backtrack/ml_history_store.py --bootstrap   # one-shot Supabase copy
    uv run python smartscore/scripts/backtrack/ml_history_store.py --verify     # counts vs Supabase
"""

import argparse
import datetime
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

_here = Path(__file__).resolve()
_root = _here.parents[3]
sys.path.insert(0, str(_root / "smartscore"))
sys.path.insert(1, str(_here.parent))

from config import SUPABASE_ADMIN_CLIENT  # noqa: E402

# The Supabase table name -> SQLite table name. The Supabase names carry the
# -prod suffix and hyphens (Postgres identifier quoting); the SQLite names
# drop both - the local store has no dev/prod axis (the dev backtrack tables
# were deleted, docs/backtrack_dev_deleted.md, and there is ONE ledger).
TABLE_MAP = {
    "Player-Snapshots-backtrack-prod": "player_snapshots_backtrack",
    "Team-Stats-backtrack-prod": "team_stats_backtrack",
    "Goalie-Snapshots-backtrack-prod": "goalie_snapshots_backtrack",
    "Player-Game-Raw-Markers-prod": "player_game_raw_markers",
}

# Primary keys, copied from the migrations. The upsert ON CONFLICT clause
# and the bootstrap idempotency both derive from these.
PRIMARY_KEYS = {
    "player_snapshots_backtrack": ("date", "player_id"),
    "team_stats_backtrack": ("season", "team_abbrev", "game_id"),
    "goalie_snapshots_backtrack": ("season", "player_id", "date"),
    "player_game_raw_markers": ("date",),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS "player_snapshots_backtrack" (
    "date" TEXT NOT NULL,
    "player_id" INTEGER NOT NULL,
    -- name is nullable HERE on purpose, unlike the Supabase migration's NOT
    -- NULL: SQLite checks NOT NULL during the INSERT stage, BEFORE the ON
    -- CONFLICT target can reroute the row to DO UPDATE - so the backfill's
    -- update lane (a top-up row carrying only the raw block, PK + 4 raw
    -- columns) could never upsert. The create lane always carries name
    -- (backfill_missing_days._record_rows), and no SET below ever touches
    -- it, so a published name can never be nulled by a top-up.
    "name" TEXT,
    "team_name" TEXT,
    "home" INTEGER,
    "gpg" REAL,
    "hgpg" REAL,
    "five_gpg" REAL,
    "hppg" REAL,
    "tgpg" REAL,
    "otga" REAL,
    "otshga" REAL,
    "injury_status" TEXT,
    "injury_desc" TEXT,
    "tims" INTEGER,
    "lineup_unit" TEXT,
    "lineup_position_group" TEXT,
    "pp_unit" TEXT,
    "lineup_status" TEXT,
    "scored" INTEGER,
    "ppg" REAL,
    "sd_shots_pg" REAL,
    "sd_goals_pg" REAL,
    "sd_toi_pg" REAL,
    "sd_sh_pct" REAL,
    "r5_shots_pg" REAL,
    "r5_goals_pg" REAL,
    "r10_shots_pg" REAL,
    "r10_goals_pg" REAL,
    "r10_toi_pg" REAL,
    "ewm_goals" REAL,
    "ewm_shots" REAL,
    "career_games" INTEGER,
    "career_goals_pg" REAL,
    "career_shots_pg" REAL,
    "team_abbrev" TEXT,
    "opponent_abbrev" TEXT,
    "game_shots" INTEGER,
    "game_goals" INTEGER,
    "game_points" INTEGER,
    "game_toi_min" REAL,
    CONSTRAINT "player_snapshots_backtrack_pkey" PRIMARY KEY ("date", "player_id")
);
CREATE INDEX IF NOT EXISTS "player_snapshots_backtrack_player_idx"
    ON "player_snapshots_backtrack" ("player_id", "date");

CREATE TABLE IF NOT EXISTS "team_stats_backtrack" (
    "season" TEXT NOT NULL,
    "team_abbrev" TEXT NOT NULL,
    "team_name" TEXT,
    "date" TEXT NOT NULL,
    "game_id" INTEGER NOT NULL,
    "tgpg" REAL,
    "otga" REAL,
    "otshga" REAL,
    CONSTRAINT "team_stats_backtrack_pkey" PRIMARY KEY ("season", "team_abbrev", "game_id")
);
CREATE INDEX IF NOT EXISTS "team_stats_backtrack_date_idx"
    ON "team_stats_backtrack" ("date");

CREATE TABLE IF NOT EXISTS "goalie_snapshots_backtrack" (
    "season" TEXT NOT NULL,
    "player_id" INTEGER NOT NULL,
    "team_abbrev" TEXT NOT NULL,
    "team_name" TEXT,
    "date" TEXT NOT NULL,
    -- Same nullable-name reasoning as player_snapshots_backtrack above.
    "name" TEXT,
    "gaa" REAL,
    "save_pct" REAL,
    "record" TEXT,
    "shutouts" INTEGER,
    "games_played" INTEGER,
    CONSTRAINT "goalie_snapshots_backtrack_pkey" PRIMARY KEY ("season", "player_id", "date")
);
CREATE INDEX IF NOT EXISTS "goalie_snapshots_backtrack_date_idx"
    ON "goalie_snapshots_backtrack" ("date");

CREATE TABLE IF NOT EXISTS "player_game_raw_markers" (
    "date" TEXT NOT NULL,
    "games" INTEGER NOT NULL,
    "player_games" INTEGER NOT NULL,
    "backfilled_at" TEXT NOT NULL,
    CONSTRAINT "player_game_raw_markers_pkey" PRIMARY KEY ("date")
);
"""


def backend():
    """'sqlite' or 'supabase' (the default - see the module docstring)."""
    return os.environ.get("ML_HISTORY_BACKEND", "supabase")


def dual_write():
    """True when the SQLite write path must mirror to Supabase (parity)."""
    return os.environ.get("ML_HISTORY_DUAL_WRITE", "0") == "1"


def db_path():
    """The SQLite file. ``ML_HISTORY_DB`` overrides (tests, experiments)."""
    override = os.environ.get("ML_HISTORY_DB")
    if override:
        return Path(override)
    return _root / "data" / "ml_history.sqlite"


def sqlite_table(supabase_table):
    """Map a Supabase table name to its SQLite twin (raise on an unknown
    name rather than silently reading the wrong store)."""
    name = TABLE_MAP.get(supabase_table)
    if name is None:
        raise RuntimeError(
            f"{supabase_table} is not part of the backtrack family the local store owns "
            f"(known: {', '.join(sorted(TABLE_MAP))})"
        )
    return name


@contextmanager
def connect(readonly=True):
    """Connection with the store's pragmas applied and the schema present.

    WAL + NORMAL sync: the daily run writes hundreds of small batches; WAL
    keeps readers unblocked and a power cut costs at most the last
    transaction - the same durability the PostgREST path had.
    """
    path = db_path()
    if readonly and not path.exists():
        raise RuntimeError(
            f"the local history store {path} does not exist yet - bootstrap it first: "
            "uv run python smartscore/scripts/backtrack/ml_history_store.py --bootstrap "
            "(or point ML_HISTORY_DB at an existing store)"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # mode=ro for reads (a write would fail loudly); a plain connect creates
    # the file for the first write, where ensure_schema runs right after.
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    else:
        con = sqlite3.connect(path, timeout=30)
    try:
        if not readonly:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.executescript(SCHEMA)
        con.row_factory = sqlite3.Row
        yield con
    finally:
        con.close()


def _coerce(row):
    """One PostgREST row -> one SQLite row: booleans to 0/1, dates stay
    TEXT. (None passes through - NULL means 'not reconstructed yet', the
    migration's own convention.)"""
    return tuple(int(v) if isinstance(v, bool) else v for v in row)


def _insert_sql(table, columns, update_cols):
    """INSERT ... ON CONFLICT DO UPDATE for one row shape.

    ``update_cols`` are the SET targets - for the update (top-up) lane that
    is exactly the row's own keys minus the PK (unmentioned columns keep
    their stored value, the PostgREST upsert semantics _record_rows'
    lane split encodes); for the create lane it is the same rule applied
    to a row that also carries identity columns."""
    cols_sql = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    pk = PRIMARY_KEYS[table]
    set_sql = ", ".join(f'"{c}" = excluded."{c}"' for c in update_cols)
    return (
        f'INSERT INTO "{table}" ({cols_sql}) VALUES ({placeholders}) '
        f"ON CONFLICT({', '.join(pk)}) DO UPDATE SET {set_sql}"
    )


def upsert_rows(supabase_table, rows, batch_size=500):
    """Insert-or-merge rows into the SQLite twin; mirror to Supabase when
    dual-write is on. Rows may mix shapes per call (unlike the PostgREST
    caller, which must split lanes - each SQLite statement only names the
    columns its own row carries). Returns rows written."""
    if not rows:
        return 0
    table = sqlite_table(supabase_table)
    pk = PRIMARY_KEYS[table]
    written = 0
    with connect(readonly=False) as con:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            # rows sharing one key signature share one INSERT statement,
            # executemany'd - same semantics as the per-row loop at a
            # fraction of the parse cost (the 624k-row bootstrap cares).
            shapes: dict[tuple, list[dict]] = {}
            for row in batch:
                shapes.setdefault(tuple(sorted(row)), []).append(row)
            for shape_rows in shapes.values():
                cols = list(shape_rows[0])
                update_cols = [c for c in cols if c not in pk]
                sql = _insert_sql(table, cols, update_cols)
                params = [_coerce([row[c] for c in cols]) for row in shape_rows]
                con.executemany(sql, params)
            con.commit()
            written += len(batch)
    if dual_write():
        _mirror_to_supabase(supabase_table, rows)
    return written


def _conflict_keys(supabase_table):
    """The on_conflict string the Supabase mirror needs: the family's
    player/team/goalie tables key (date, player_id)-shaped PKs, the marker
    shelf keys date alone."""
    return "date" if supabase_table.endswith("Markers-prod") else "date,player_id"


def _mirror_to_supabase(supabase_table, rows):
    """Dual-write's Supabase leg: same upsert the pre-migration code ran,
    lane-shaped by the caller. A failure prints LOUD and continues - the
    mirror is the parity convenience, never the source of truth."""
    try:
        # group by key signature: PostgREST 400s a mixed-shape batch
        # (an absent key would be an explicit NULL there)
        shapes: dict[tuple, list[dict]] = {}
        for row in rows:
            shapes.setdefault(tuple(sorted(row)), []).append(row)
        conflict = _conflict_keys(supabase_table)
        for batch_rows in shapes.values():
            for start in range(0, len(batch_rows), 500):
                SUPABASE_ADMIN_CLIENT.table(supabase_table).upsert(
                    batch_rows[start : start + 500], on_conflict=conflict, returning="minimal"
                ).execute()
    except Exception as exc:  # noqa: BLE001 - mirror failures must not kill the local run
        print(
            f"WARNING: dual-write mirror to {supabase_table} failed ({type(exc).__name__}: {str(exc)[:240]}) - "
            "SQLite rows are committed; the Supabase copy is now behind",
            flush=True,
        )


def marker_upsert(supabase_marker_table, row):
    """One backfill marker row (PK: date). ``backfilled_at`` is stamped
    here, matching the column's DEFAULT now() on Supabase."""
    payload = dict(row)
    payload.setdefault("backfilled_at", datetime.datetime.now(datetime.UTC).isoformat())
    return upsert_rows(supabase_marker_table, [payload])


def fetch_window(supabase_table, columns, lo, hi, order_cols):
    """``date >= lo AND date < hi`` ordered read -> DataFrame.

    One query - the month-window pagination model.fetch_table documents is
    a PostgREST statement-timeout (57014) workaround a local index has no
    need for. The half-open ``hi`` boundary is the caller's contract (the
    pick date's own post-game rows never leak into pre-game features)."""
    table = sqlite_table(supabase_table)
    cols_sql = "*" if columns == ["*"] else ", ".join(f'"{c}"' for c in columns)
    order_sql = ", ".join(f'"{c}"' for c in order_cols)
    query = f'SELECT {cols_sql} FROM "{table}" WHERE "date" >= ? AND "date" < ? ORDER BY {order_sql}'
    with connect() as con:
        frame = pd.read_sql_query(query, con, params=(lo, hi))
    if frame.empty:
        return pd.DataFrame(columns=columns)
    return frame


def first_date(supabase_table):
    """Earliest published date (the history fetch always starts here)."""
    with connect() as con:
        row = con.execute(f'SELECT MIN("date") AS d FROM "{sqlite_table(supabase_table)}"').fetchone()
    return row["d"]


def latest_date(supabase_table):
    """Newest published date, or None when empty/not bootstrapped (the
    backfill's empty-ledger gate treats both the same way - its fail-loud
    message names the bootstrap command)."""
    path = db_path()
    if not path.exists():
        return None
    with connect() as con:
        row = con.execute(f'SELECT MAX("date") AS d FROM "{sqlite_table(supabase_table)}"').fetchone()
    return row["d"]


def existing_player_keys(supabase_table, day):
    """player_id set the store owns for one date (backfill's lane split)."""
    with connect() as con:
        rows = con.execute(
            f'SELECT "player_id" FROM "{sqlite_table(supabase_table)}" WHERE "date" = ?', (day,)
        ).fetchall()
    return {int(r["player_id"]) for r in rows}


def marked_dates(supabase_marker_table, after_date):
    """Dates the marker shelf records as fully backfilled (>= after_date).
    An absent file simply means no markers - every date is attempted, and
    the upserts are idempotent by PK either way."""
    path = db_path()
    if not path.exists():
        return set()
    with connect() as con:
        rows = con.execute(
            f'SELECT "date" FROM "{sqlite_table(supabase_marker_table)}" WHERE "date" >= ?', (after_date,)
        ).fetchall()
    return {r["date"] for r in rows}


def latest_rows_per_player(supabase_table, columns, player_ids, chunk=200):
    """Each player's latest row carrying ``columns`` - run_daily_picks.
    _latest_shot_columns' fallback read, local form (order in SQL, tail(1)
    in pandas exactly as the Supabase path does)."""
    table = sqlite_table(supabase_table)
    cols_sql = ", ".join(f'"{c}"' for c in columns)
    frames = []
    with connect() as con:
        for start in range(0, len(player_ids), chunk):
            ids = [int(pid) for pid in player_ids[start : start + chunk]]
            marks = ", ".join("?" for _ in ids)
            rows = con.execute(
                f'SELECT {cols_sql} FROM "{table}" WHERE "player_id" IN ({marks}) ORDER BY "date"', ids
            ).fetchall()
            if rows:
                frames.append(pd.DataFrame([dict(r) for r in rows]))
    if not frames:
        return pd.DataFrame(columns=columns)
    frame = pd.concat(frames, ignore_index=True)
    frame["player_id"] = pd.to_numeric(frame["player_id"])
    return frame.sort_values("date").groupby("player_id").tail(1)


def count(supabase_table):
    with connect() as con:
        return con.execute(f'SELECT COUNT(*) AS n FROM "{sqlite_table(supabase_table)}"').fetchone()["n"]


# ----------------------------------------------------------- bootstrap copy

BOOTSTRAP_PAGE = 1000


def _supabase_pages(supabase_table, delay=0.0):
    """Yield the whole Supabase table as offset pages of dicts.

    Plain ascending offset paging (no ordering column): PostgREST pages by
    offset against the table's natural scan, and the one-shot bootstrap is
    the only consumer - model.fetch_table's month windows exist for the
    daily path, not here. ``delay`` throttles for politeness."""
    offset = 0
    while True:
        page = SUPABASE_ADMIN_CLIENT.table(supabase_table).select("*").range(offset, offset + BOOTSTRAP_PAGE - 1)
        rows = page.execute().data
        if not rows:
            return
        yield rows
        offset += len(rows)
        if delay:
            time.sleep(delay)


def bootstrap(supabase_table, batch_size=1000, delay=0.0):
    """Copy one Supabase table into the local store (idempotent upserts).

    This is the ONE-TIME pull (plan Phase 0): ~624k rows lands as roughly
    625 pages; at PostgREST latency that is minutes, and the 560 MB read is
    a fraction of the 5 GB monthly egress cap. Re-running is safe - every
    page upserts on the same PK."""
    table = sqlite_table(supabase_table)
    pk = PRIMARY_KEYS[table]
    total = 0
    started = time.time()
    with connect(readonly=False) as con:
        for pages in _supabase_pages(supabase_table, delay=delay):
            shapes: dict[tuple, list[dict]] = {}
            for row in pages:
                shapes.setdefault(tuple(sorted(row)), []).append(row)
            for shape_rows in shapes.values():
                cols = list(shape_rows[0])
                update_cols = [c for c in cols if c not in pk]
                sql = _insert_sql(table, cols, update_cols)
                params = [_coerce([row[c] for c in cols]) for row in shape_rows]
                con.executemany(sql, params)
            con.commit()
            total += len(pages)
            print(f"  {supabase_table}: {total:,} rows so far ({time.time() - started:.0f}s)", flush=True)
    return total


def verify(supabase_table):
    """Local row count + date span vs Supabase's - the bootstrap's receipt.
    Supabase counts with ``count=exact`` (a head request, no rows pulled)."""
    local_n = count(supabase_table)
    remote = SUPABASE_ADMIN_CLIENT.table(supabase_table).select("*", count="exact").limit(0).execute()
    remote_n = remote.count or 0
    local_lo = first_date(supabase_table)
    local_hi = latest_date(supabase_table)
    print(
        f"{supabase_table}: local {local_n:,} rows [{local_lo}..{local_hi}] vs Supabase {remote_n:,} "
        f"-> {'MATCH' if local_n == remote_n else 'MISMATCH'}",
        flush=True,
    )
    return local_n == remote_n


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="one-shot copy of the backtrack family from Supabase into data/ml_history.sqlite",
    )
    parser.add_argument("--verify", action="store_true", help="row-count + date-span check against Supabase")
    parser.add_argument("--table", choices=sorted(TABLE_MAP), help="a single table (default: the whole family)")
    parser.add_argument("--delay", type=float, default=0.0, help="seconds between Supabase pages (politeness)")
    args = parser.parse_args(argv)

    if not args.bootstrap and not args.verify:
        parser.error("nothing to do - pass --bootstrap and/or --verify")
    targets = [args.table] if args.table else sorted(TABLE_MAP)

    ok = True
    if args.bootstrap:
        for t in targets:
            n = bootstrap(t, delay=args.delay)
            print(f"bootstrapped {t}: {n:,} rows", flush=True)
    if args.verify:
        for t in targets:
            ok = verify(t) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
