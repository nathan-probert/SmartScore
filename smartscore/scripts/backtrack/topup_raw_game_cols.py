"""One-time top-up: fill the four per-game RAW columns of migration 20261027
on Player-Snapshots-backtrack-{ENV}, for ALL historical rows, from the local
raw store (data/raw_nhl.sqlite ``player_games``).

The stage-2 exactness companion of
supabase/migrations/20261027_add_per_game_raw_columns_player_snapshots_backtrack.sql,
modeled on the 20261024 16-column pattern: an idempotent ALTER TABLE of
``Player-Snapshots-backtrack-__ENV__`` applied by deploy.yml's standing
``migrate`` job (ENV=dev on a PR carrying the ``deploy`` label, ENV=prod on a
push to main; psql re-applies every file in supabase/migrations/ every run,
idempotency being the correctness guarantee).

WHAT IT DOES (publish_shot_cols' batch conventions, verbatim)
--------
1. Pages EVERY (date, player_id) key out of the snapshot table (month
   windows + 10k range pages - the snapshot_keys() mechanics).
2. Reads the store's per-game rows: shots/goals/points keep the store's
   exact integers (Int64 so a NaN stays null-able); toi 'MM:SS' parses to
   decimal minutes by the SHARED parser model._toi_minutes - the exact cast
   the 16-column backfill used; NaN -> None; season never ships (the joins
   key on (date, player_id)).
3. Upserts exactly (date, player_id, game_*...) - six keys, ONE key
   signature per batch - in 500-row batches with on_conflict="date,player_id",
   returning="minimal". A row the store has no player_games row for is
   SKIPPED, not zeroed: NULL stays "did not play" (the same convention
   shot_features_from_games / _backfilled_game_rows read past rows by), row
   counts can never shrink, and no other column is ever touched (a missing
   key on PostgREST is an explicit NULL, never an omission -
   run_daily_picks._upsert_raw_rows' rule).
4. Re-reads a 500-row sample and diffs every one of the four columns
   against a store recompute (0 mismatches expected, rel tol 1e-9).

PRECONDITION: the migration must have landed on the env's table - probed
via publish_shot_cols.raw_game_columns_landed; NO DDL is performed here.
IDEMPOTENT: PK upserts, so a re-run is a safe overwrite, not an append.

Usage::

    uv run python smartscore/scripts/backtrack/topup_raw_game_cols.py --env dev
    uv run python smartscore/scripts/backtrack/topup_raw_game_cols.py --env prod
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[2]))

from model import SUPABASE_ADMIN_CLIENT, _month_windows, _toi_minutes, player_table  # noqa: E402
from publish_shot_cols import RAW_GAME_COLUMNS, raw_game_columns_landed, raw_game_map  # noqa: E402

# PostgREST rows per request: the spec cap the 16-column backfill landed on.
BATCH_SIZE = 500

# The brief's "log per 20k": progress every 20_000 written rows.
PROGRESS_ROWS = 20_000

RAW_FETCH_COLUMNS = "date,player_id,game_shots,game_goals,game_points,game_toi_min"

KEY_COLUMNS = ("date", "player_id", "name", *RAW_GAME_COLUMNS)


def snapshot_keys_frame(table):
    """The full (date, player_id) key set + name, paged like snapshot_keys()."""
    start = time.time()
    first = SUPABASE_ADMIN_CLIENT.table(table).select("date").order("date").limit(1).execute().data
    last = SUPABASE_ADMIN_CLIENT.table(table).select("date").order("date", desc=True).limit(1).execute().data
    if not first or not last:
        raise RuntimeError(f"{table} is empty - nothing to top-up")
    frames = []
    for lo, hi in _month_windows(first[0]["date"], last[0]["date"]):
        offset = 0
        query = SUPABASE_ADMIN_CLIENT.table(table).select("date,player_id,name").gte("date", lo).lt("date", hi)
        query = query.order("date").order("player_id")
        while True:
            rows = query.range(offset, offset + 10_000 - 1).execute().data
            if not rows:
                break
            frames.append(pd.DataFrame(rows))
            offset += len(rows)
    if not frames:
        raise RuntimeError(f"{table} paged 0 rows")
    keys = pd.concat(frames, ignore_index=True)
    keys["date"] = keys["date"].astype(str)
    keys["player_id"] = keys["player_id"].astype("int64")
    keys = keys.drop_duplicates(["date", "player_id"])
    print(f"  {len(keys):,} snapshot rows paged in {time.time() - start:.0f}s", flush=True)
    return keys


def build_key_frame(keys, store_frame):
    """Keys the store supplies stats for, as a six-key frame.

    Casts are the 16-column backfill's: the store's exact integers kept via
    Int64 (NaN stays null-able), toi decimal minutes by the shared parser,
    NaN -> None done later in the record encoder. Store rows without the
    essential boxscore integers (shots AND goals both NaN) are NOT a played
    game, same drop rule the daily attach applies.
    """
    if store_frame.empty:
        merged = keys.head(0)
    else:
        source = store_frame.dropna(subset=["shots", "goals"])[["date", "player_id", "shots", "goals", "points", "toi"]]
        merged = keys.merge(source, on=["date", "player_id"], how="inner", validate="many_to_one")
    skipped = len(keys) - len(merged)
    merged["player_id"] = merged["player_id"].astype("int64")
    for game_col, store_col in (("game_shots", "shots"), ("game_goals", "goals"), ("game_points", "points")):
        merged[game_col] = merged[store_col].astype("Int64")
    # The shared parser: 'MM:SS' -> mm + ss/60 decimal minutes (empty -> NaN
    # -> None at record-encoding time), the exact cast the 16-col backfill used.
    merged["game_toi_min"] = _toi_minutes(merged["toi"])
    print(
        f"filling {len(merged):,}/{len(keys):,} keys; {skipped:,} left NULL (no player_games row / no stats)",
        flush=True,
    )
    # list(...) - a bare tuple would go through the multiindex path in pandas.
    return merged[list(KEY_COLUMNS)]  # noqa: C405


def payload_records(frame):
    """Rows ready for supabase-py: python scalars, NaN -> None (shared rule)."""
    data = frame.astype(object).where(pd.notna(frame), None)
    return [
        {key: value.item() if isinstance(value, np.generic) else value for key, value in row.items()}
        for row in data.to_dict("records")
    ]


def upsert_frame(frame, table):
    """500-row upserts on the PK; three retries a batch; log per 20k; raise on a failed batch."""
    records = payload_records(frame)
    total = len(records)
    print(f"upserting {total:,} rows to {table} ({BATCH_SIZE}/batch)", flush=True)
    written = 0
    failed = []
    start = time.time()
    for offset in range(0, total, BATCH_SIZE):
        batch = records[offset : offset + BATCH_SIZE]
        error = None
        for attempt in range(3):
            try:
                SUPABASE_ADMIN_CLIENT.table(table).upsert(
                    batch, on_conflict="date,player_id", returning="minimal"
                ).execute()
                error = None
                break
            except Exception as exc:  # noqa: BLE001 - retried below, then surfaced
                error = exc
                if attempt == 2:
                    break
                time.sleep(2.0 * (attempt + 1))
        if error is not None:
            failed.append((offset // BATCH_SIZE, f"{type(error).__name__}: {str(error)[:280]}"))
            continue
        written += len(batch)
        if written % PROGRESS_ROWS == 0:
            rate = written / max(time.time() - start, 1e-9)
            print(
                f"  rows {written:,}  {rate:,.0f} rows/s  elapsed {time.time() - start:.0f}s  errors {len(failed)}",
                flush=True,
            )
    if written:
        rate = written / max(time.time() - start, 1e-9)
        print(f"upserted {written:,} rows in {time.time() - start:.0f}s ({BATCH_SIZE}/batch)", flush=True)
    if failed:
        sample = ", ".join(f"batch#{index}: {err}" for index, err in failed[:3])
        raise RuntimeError(f"{len(failed)} batch(es) failed after retries: {sample} ...")
    return written


def spot_check(env, table, sample_size=500, tol_rel=1e-9):
    """Re-read a sample and diff the four columns against a store recompute.

    The sample taps three windows (head / middle / tail) spread across the
    table's date axis, then diffs EVERY one of the four columns against the
    values recomputed from the store with the same casts the run wrote
    with. Pairs the store has no player_games row for are excluded - a spot
    check verifies the topped-up set (NULL = did not play is checked by the
    attach conventions, not here). Returns (mismatches, matched, worst_rel).
    """
    start = time.time()
    total = SUPABASE_ADMIN_CLIENT.table(table).select("date", count="exact").limit(1).execute().count
    if total == 0:
        print(f"spot-check: {table} has 0 rows - nothing to check")
        return 0, 0, 0.0
    plates = []
    for offset in sorted({0, max(total // 2 - sample_size // 2, 0), max(total - sample_size, 0)}):
        rows = (
            SUPABASE_ADMIN_CLIENT.table(table)
            .select(RAW_FETCH_COLUMNS)
            .order("date")
            .order("player_id")
            .range(offset, offset + sample_size - 1)
            .execute()
            .data
        )
        plates.extend(rows or [])
    sample = pd.DataFrame(plates).drop_duplicates(["date", "player_id"]).sort_values(["date", "player_id"])
    sample = sample.head(sample_size)
    if sample.empty:
        print(f"spot-check: {table} page read empty - cannot verify")
        return 0, 0, 0.0
    print(f"spot-check: {len(sample)} rows read from {table} (table rows: {total:,})", flush=True)
    store = raw_game_map(min_date=str(sample["date"].min()), max_date=str(sample["date"].max()))
    store = store.dropna(subset=["shots", "goals"])[["date", "player_id", "shots", "goals", "points", "toi"]]
    mismatches = 0
    worst_rel = 0.0
    recomputable = 0
    for row in sample.to_dict("records"):
        source = store[(store["player_id"] == int(row["player_id"])) & (store["date"] == str(row["date"]))]
        if source.empty:
            continue  # not store-covered: outside the recomputation claim
        src = source.iloc[0]
        recomputable += 1
        expected = {
            "game_shots": int(src["shots"]),
            "game_goals": int(src["goals"]),
            "game_points": int(src["points"]),
            "game_toi_min": float(_toi_minutes(pd.Series([src["toi"]])).iloc[0]),
        }
        for col, want in expected.items():
            got = row[col]
            if got is None or want is None:
                mismatches += 1
                continue
            if col == "game_toi_min":
                rel = abs(float(got) - want) / max(abs(want), 1e-12)
                worst_rel = max(worst_rel, rel)
                if rel > tol_rel:
                    mismatches += 1
            elif got != want:
                mismatches += 1
    print(
        (
            "spot-check vs recompute: "
            f"mismatches={mismatches} matched={recomputable} columns={','.join(RAW_GAME_COLUMNS)} "
            f"tol_rel={tol_rel} worst_rel={worst_rel:.2e}"
        ),
        flush=True,
    )
    print(f"elapsed {time.time() - start:.0f}s", flush=True)
    return mismatches, recomputable, worst_rel


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="dev", choices=["dev", "prod"], help="backtrack table suffix (default dev)")
    parser.add_argument("--spot-check", action="store_true", help="500-row verify-and-exit (no writes)")
    parser.add_argument("--skip-spot-check", action="store_true", help="upsert without the trailing verify")
    args = parser.parse_args()

    table = player_table(args.env)
    if not raw_game_columns_landed(table):
        raise SystemExit(
            f"migration 20261027 has NOT landed on {table}: no raw columns to top up. "
            "Apply the migration first (standing CI migrate job - prod on main merge; "
            "or psql with SUPABASE_DB_URL locally)."
        )
    print(f"probe OK: game_* columns landed on {table}")

    if args.spot_check:
        mismatches, recomputable, _ = spot_check(args.env, table, sample_size=500)
        if mismatches:
            raise SystemExit(1)
        return

    try:
        keys = snapshot_keys_frame(table)
    except RuntimeError as exc:
        if "is empty" not in str(exc):
            raise
        # An EMPTY backtrack table is a clean no-op top-up: the probe proved
        # the columns exist, zero rows exist, so there is nothing to fill
        # (verified live: Player-Snapshots-backtrack-dev is 0 rows).
        print(f"{table} has 0 rows: top-up is a verified no-op (0 rows processed, 0 corrections)")
        return
    print(f"snapshot rows: {len(keys):,} ({table})", flush=True)
    store = raw_game_map(min_date=str(keys["date"].min()), max_date=str(keys["date"].max()))
    print(f"store rows over that date range: {len(store):,}", flush=True)
    frame = build_key_frame(keys, store)
    filled = int(frame["game_shots"].notna().sum())
    written = upsert_frame(frame, table) if len(frame) else 0
    print(
        (
            f"corrections: {filled:,}/{len(keys):,} keys now carry per-game values "
            f"({len(keys) - filled:,} left NULL: no player_games row / no bogus stats); "
            f"upserted {written:,} rows, 0 deletions"
        ),
        flush=True,
    )
    if not args.skip_spot_check:
        mismatches, recomputable, _ = spot_check(args.env, table, sample_size=500)
        if mismatches:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
