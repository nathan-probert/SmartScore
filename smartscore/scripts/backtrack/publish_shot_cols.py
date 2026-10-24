"""One-time backfill: publish the 16 shot/TOI + club columns to Player-Snapshots-backtrack-{ENV}.

The teammate of supabase/migrations/20261024_add_shot_columns_player_snapshots_backtrack.sql
and the implementation of docs/supabase_shot_columns.md option 1: make the
in-cloud ML Lambda's DATASET_FEATURES readable from Supabase alone.

WHAT IT DOES
------------
1. Runs ``model.shot_features()`` VERBATIM over the raw store
   (data/raw_nhl.sqlite) - the sorted ``(player_id, date, season)`` frames,
   ``shift(1)``, ``rolling(...)``, ``ewm(halflife=5)`` and expanding means are
   never re-derived here, because a naive SQL re-implementation is where the
   silent decimal drift lives.
2. Joins ``team_abbrev`` / ``opponent_abbrev`` per ``(date, player_id)`` - the
   club_map() join minus the unused columns.
3. Restricts to ``(date, player_id)`` pairs that exist in the Supabase snapshot
   table (publish never wrote the store's untracked ~1%).
4. Upserts 16 columns (+ the key) in 500-row batches with
   ``on_conflict="date,player_id"``, ``returning="minimal"`` - identical to
   local_store.publish(). NaN goes to None first: supabase-py + PostgREST
   reject a bare ``NaN`` and the whole batch dies with it.

IDEMPOTENT: keys on the PK, so a re-run is a safe overwrite, not an append.
Re-runs are encouraged after any interrupted attempt.

Dtypes (spec §1 / §5): all shot/TOI columns are DOUBLE PRECISION floats,
``career_games`` INTEGER (exact ints), TOI is decimal minutes (mm:ss ->
mm + ss/60, done inside shot_features()). ``season`` normalization
(``astype("Int64").astype(str)``) is irrelevant here because the payload never
ships a season string - the internal joins key on (date, player_id) and the
sortable order inside shot_features() already matches the spec's rule.

Usage::

    uv run python smartscore/scripts/backtrack/publish_shot_cols.py --dry-run
    uv run python smartscore/scripts/backtrack/publish_shot_cols.py --env dev
    uv run python smartscore/scripts/backtrack/publish_shot_cols.py --env prod
"""

import argparse
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[2]))

from model import (  # noqa: E402
    RAW_DB,
    SHOT_FEATURES,
    SUPABASE_ADMIN_CLIENT,
    _month_windows,
    player_table,
    shot_features,
)

CLUB_TABLE_COLS = (
    "opponent_abbrev",
    "team_abbrev",
)  # Block B semantics; the payload ships both key columns first.

SHOT_TABLE_COLS = tuple(SHOT_FEATURES)  # Block A, in SHOT_FEATURES order.

PAYLOAD_COLUMNS = (
    "date",
    "player_id",
    "name",
    *SHOT_TABLE_COLS,
    *CLUB_TABLE_COLS,
)


def club_map_cols(db_path=RAW_DB):
    """(date, player_id) -> team_abbrev, opponent_abbrev from the raw store.

    Same join keys club_map() uses; DISTINCT as a cheap duplicate guard, with a
    hard check that stays true (it must: club_map validates many_to_one).
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    frame = pd.read_sql_query(
        "SELECT DISTINCT game_date AS date, player_id, team_abbrev, opponent_abbrev FROM player_games",
        con,
    )
    con.close()
    frame["date"] = frame["date"].astype(str)
    dup = frame.duplicated(["date", "player_id"]).any()
    if dup:
        raise RuntimeError("player_games has duplicate (game_date, player_id) club rows")
    return frame


def _last_date(table):
    last = SUPABASE_ADMIN_CLIENT.table(table).select("date").order("date", desc=True).limit(1).execute().data
    if not last:
        raise RuntimeError(f"{table} is empty - wrong --env, or nothing was published to it")
    return last[0]["date"]


def snapshot_keys(env):
    """All (date, player_id, name) rows already published to the snapshot table.

    ``name`` rides along because PostgREST evaluates NOT NULL constraints on
    the upsert payload even when every payload row hits an existing PK row
    (verified: a keys-only upsert 400s with "null value in column \"name\"",
    the same sparse request with the row's own name succeeds). publish()
    sidesteps this by always sending a full-row payload.
    """
    table = player_table(env)
    start = time.time()
    first = SUPABASE_ADMIN_CLIENT.table(table).select("date").order("date").limit(1).execute().data
    if not first:
        raise RuntimeError(f"{table} is empty - wrong - wrong env, or nothing was published to it")
    last = _last_date(table)
    frames = []
    for lo, hi in _month_windows(first[0]["date"], last):
        offset = 0
        query = SUPABASE_ADMIN_CLIENT.table(table).select("date,player_id,name").gte("date", lo).lt("date", hi)
        query = query.order("date").order("player_id")
        while True:
            rows = query.range(offset, offset + 10_000 - 1).execute().data
            if not rows:
                break
            frames.append(pd.DataFrame(rows))
            offset += len(rows)
    keys = pd.concat(frames, ignore_index=True)
    keys["date"] = keys["date"].astype(str)
    keys["player_id"] = keys["player_id"].astype("int64")
    keys = keys.drop_duplicates(["date", "player_id"])
    if keys["name"].isna().any():
        raise RuntimeError(f"{int(keys['name'].isna().sum())} snapshot rows have null name - NOT NULL column")
    print(f"  {len(keys):,} snapshot rows in {time.time() - start:.0f}s", flush=True)
    return keys


def build_payload(env, limit=None):
    """Frame of the 16 published columns joined with the table's existing keys."""
    frame = shot_features()
    print(f"shot_features: {len(frame):,} player-game rows", flush=True)
    frame = frame.merge(club_map_cols(), on=["date", "player_id"], how="left", validate="many_to_one")
    keys = snapshot_keys(env)
    before = len(frame)
    frame = keys.merge(frame, on=["date", "player_id"], how="inner", validate="many_to_one")
    print(f"restricted to published keys: {before:,} -> {len(frame):,} rows", flush=True)
    # Some raw rows have no opponent_abbrev because the NHL game-log payload
    # missed that field (32,251 store rows; snapshot overlap checked to be
    # 32 of 617,310 = 0.005% on dev). club_map() merges them as a null club
    # locally and the opp joins produce NaN features for them - publishing
    # NULL is the same fact, not a loss. Anything else missing is an error.
    null_opp = frame["opponent_abbrev"].isna().sum()
    if null_opp:
        print(f"note: {null_opp} rows publish NULL opponent_abbrev (source game-log lacked it)")
    if frame["team_abbrev"].isna().any():
        raise RuntimeError(
            f"{int(frame['team_abbrev'].isna().sum())} published snapshot rows have no team_abbrev in player_games"
        )
    if limit:
        frame = frame.head(limit)
    if frame["career_games"].isna().any():
        raise RuntimeError("career_games must never be null (0 on debut)")
    frame["player_id"] = frame["player_id"].astype("int64")
    frame["name"] = frame["name"].astype(str)
    frame["career_games"] = frame["career_games"].astype("int64")
    # list(...) - a bare tuple would go through the multiindex path in pandas.
    return frame[list(PAYLOAD_COLUMNS)]


def payload_records(frame):
    """Rows ready for supabase-py: python scalars, NaN -> None.

    The astype(object) is load-bearing: ``where(cond, None)`` on float columns
    coerces the fill back to NaN (and supabase-py then dies on json "nan"),
    while an object column keeps the None.
    """
    data = frame.astype(object).where(pd.notna(frame), None)
    return [
        {key: value.item() if isinstance(value, np.generic) else value for key, value in row.items()}
        for row in data.to_dict("records")
    ]


def upsert_frame(frame, env, batch_size=500, concurrency=1, progress_every=50):
    """Upsert the payload in batches; logs every ``progress_every`` chunks."""
    table = player_table(env)
    records = payload_records(frame)
    print(f"upserting {len(records):,} rows to {table} ({batch_size}/batch)", flush=True)
    batches = [records[start : start + batch_size] for start in range(0, len(records), batch_size)]
    written = 0
    failed = []
    start = time.time()

    def send(index_batch):
        index, batch = index_batch
        for attempt in range(3):
            try:
                SUPABASE_ADMIN_CLIENT.table(table).upsert(
                    batch, on_conflict="date,player_id", returning="minimal"
                ).execute()
                return index, len(batch), None
            except Exception as exc:  # noqa: BLE001 - retried below, then surfaced
                if attempt == 2:
                    return index, 0, f"{type(exc).__name__}: {str(exc)[:280]}"
                time.sleep(2.0 * (attempt + 1))

    scheduled = list(enumerate(batches))

    if concurrency <= 1:
        iterator = (send(pair) for pair in scheduled)
    else:
        executor = ThreadPoolExecutor(max_workers=concurrency)
        iterator = executor.map(send, scheduled)

    done = 0
    for index, count, error in iterator:
        written += count
        done += 1
        if error:
            failed.append((index, error))
        if done % progress_every == 0 or done == len(batches):
            rate = written / max(time.time() - start, 1e-9)
            print(
                f"  batch {done:,}/{len(batches):,}  rows {written:,}  "
                f"{rate:,.0f} rows/s  errors {len(failed)}",
                flush=True,
            )
    if failed:
        sample = ", ".join(f"batch#{i}: {e}" for i, e in failed[:3])
        raise RuntimeError(f"{len(failed)} batch(es) failed after retries: {sample} ...")
    print(f"upserted {written:,} rows in {time.time() - start:.0f}s ({batch_size}/batch)", flush=True)
    return written


def dry_run(frame):
    """The acceptance facts a human should see before a real upsert."""
    print("\n=== dry run ===")
    print(f"rows: {len(frame):,}  columns: {list(frame.columns)}")
    for col in frame.columns:
        if col in ("date", "player_id"):
            continue
        notna = frame[col].notna().sum()
        print(f"  {col}: {notna:,} non-null ({100 * notna / len(frame):.1f}%)", flush=True)
    print("dtype check: career_games", frame["career_games"].dtype)
    plaus = [
        ("sd_shots_pg", 0.0, 10.0),
        ("sd_goals_pg", 0.0, 5.0),
        ("sd_sh_pct", 0.0, 1.0),
        ("r10_toi_pg", 0.0, 40.0),
    ]
    for col, lo, hi in plaus:
        series = frame[col].dropna()
        inside = series.between(lo, hi).mean()
        print(f"  {col}: {100 * inside:.3f}% inside [{lo}, {hi}] (season-first rows are NaN by design)")
    sample = frame.dropna(subset=["sd_shots_pg"]).head(3)
    print(sample[["date", "player_id", "sd_shots_pg", "team_abbrev"]].to_string(index=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="dev", choices=["dev", "prod"], help="backtrack table suffix (default dev)")
    parser.add_argument("--dry-run", action="store_true", help="print coverage/dtypes and exit without writing")
    parser.add_argument("--limit", type=int, default=None, help="cap the number of rows (smoke test)")
    parser.add_argument(
        "--batch-size", type=int, default=500, help="PostgREST rows per request (default 500, spec cap)"
    )
    parser.add_argument("--concurrency", type=int, default=1, help="parallel batch requests (default 1 = serial)")
    parser.add_argument("--progress-every", type=int, default=50, help="log every N batches")
    args = parser.parse_args()
    if args.batch_size > 1000:
        raise SystemExit("PostgREST's hard request cap is 1000 rows; keep --batch-size smaller")

    frame = build_payload(args.env, limit=args.limit)
    if args.dry_run:
        dry_run(frame)
        return
    upsert_frame(frame, args.env, batch_size=args.batch_size, concurrency=args.concurrency)


if __name__ == "__main__":
    main()


def columns_landed(table):
    """True once the shot-columns migration has created the new columns.

    Probing a known new column is the cheapest owner-independent check: until
    the DDL lands, PostgREST 400s ANY request naming it, so the caller must
    not yet send the keys (spec gotcha 5 - unknown keys drop the whole batch).
    """
    try:
        SUPABASE_ADMIN_CLIENT.table(table).select("sd_shots_pg").limit(1).execute()
        return True
    except Exception:  # noqa: BLE001 - any failure means "not landed yet"
        return False


_PROBE = {}


def attach_shot_columns(payload, table, block=None):
    """Add the 16 published columns onto a daily upsert payload in place.

    ``payload`` is local_store.publish()'s row-dict list for one season. The
    values come from the same verbatim shot_features() run the backfill used,
    so a nightly row and its owner-migrated history row carry identical
    semantics. Column values keep their dtypes: floats for the shot/TOI
    aggregates, exact int for career_games, None for NaN (season-first rows,
    and the 32 rows whose source game-log lacked opponentAbbrev).

    After the first successful attach for a table the probe result is cached;
    until the migration has landed the payload returns unchanged.
    """
    placed = _PROBE.get(table)
    if placed is None:
        placed = _PROBE[table] = columns_landed(table)
        if not placed:
            print(f"shot-columns migration not landed on {table}; skipping the 16 keys")
    if not placed or not payload:
        return payload

    import pandas as pd  # noqa: PLC0415 - keep the hot nightly path import-light

    if block is None:
        frame = shot_features().merge(
            club_map_cols(), on=["date", "player_id"], how="left", validate="many_to_one"
        )
        frame["player_id"] = frame["player_id"].astype("int64")
        frame["career_games"] = frame["career_games"].astype("int64")
    else:
        frame = block

    frame = frame.astype(object).where(pd.notna(frame), None)
    by_key = {}
    for row in frame.drop(columns=["game_id"], errors="ignore").to_dict("records"):
        # Every published column must exist on the frame (the migration's
        # column set); a block missing one is a contract break, so KeyError is
        # the loud failure we want.
        by_key[(row["date"], int(row["player_id"]))] = {
            col: row[col].item() if isinstance(row[col], np.generic) else row[col]
            for col in PAYLOAD_COLUMNS
            if col not in ("date", "player_id", "name")
        }
    hits = 0
    for row in payload:
        key = (row["date"], int(row["player_id"]))
        values = by_key.get(key)
        if values is None:
            continue
        row.update(values)
        hits += 1
    print(f"shot/TOI + club columns attached to {hits}/{len(payload)} payload row(s)")
    return payload
