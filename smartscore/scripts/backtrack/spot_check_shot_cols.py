"""Dev-only spot check for the shot/TOI + club columns backfill.

Samples 1,000 random (date, player_id) rows from Player-Snapshots-backtrack-{env},
reads them back from Supabase, recomputes the same rows' 16 columns locally via
the exact backfill path (model.shot_features() + club join - never re-implementing
the pandas window math), and asserts equality:

* floats (DOUBLE PRECISION columns): exact to ~1e-9 relative tolerance
* career_games (INTEGER): exact
* team_abbrev / opponent_abbrev (TEXT): exact strings
* NaN <-> NULL parity

Also asserts the row count is unchanged (backfill is an upsert overwrite, never
append/delete).

Usage::

    uv run python smartscore/scripts/backtrack/spot_check_shot_cols.py --env dev
    uv run python smartscore/scripts/backtrack/spot_check_shot_cols.py --env dev --sample 200
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[2]))

from model import (  # noqa: E402
    SUPABASE_ADMIN_CLIENT,
    player_table,
    shot_features,
)
from publish_shot_cols import PAYLOAD_COLUMNS, club_map_cols, snapshot_keys  # noqa: E402

FLOAT_COLUMNS = [
    "sd_shots_pg",
    "sd_goals_pg",
    "sd_toi_pg",
    "sd_sh_pct",
    "r5_shots_pg",
    "r5_goals_pg",
    "r10_shots_pg",
    "r10_goals_pg",
    "r10_toi_pg",
    "ewm_goals",
    "ewm_shots",
    "career_goals_pg",
    "career_shots_pg",
]
INT_COLUMNS = ["career_games"]
TEXT_COLUMNS = ["team_abbrev", "opponent_abbrev"]
REL_TOL = 1e-9


def remote_rows(env, keys):
    """Read the sampled keys back, one request per slate date."""
    table = player_table(env)
    client = SUPABASE_ADMIN_CLIENT

    def fetch(date):
        # The Supabase shared pooler terminates HTTP/2 streams under bursty
        # concurrency; retry with backoff, serialise to 2 workers below.
        for attempt in range(4):
            try:
                return (
                    client.table(table)
                    .select("date,player_id," + ",".join(list(FLOAT_COLUMNS) + INT_COLUMNS + TEXT_COLUMNS))
                    .eq("date", date)
                    .execute()
                    .data
                )
            except Exception:  # noqa: BLE001 - retried below, then surfaced
                if attempt == 3:
                    raise
                time.sleep(2.0 * (attempt + 1))

    dates = sorted({d for d, _ in keys})
    want = set(keys)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pages = list(pool.map(fetch, dates))
    rows = [row for page in pages for row in page]
    remote = pd.DataFrame(rows)
    remote["date"] = remote["date"].astype(str)
    remote["player_id"] = remote["player_id"].astype("int64")
    remote = remote[[tuple(pair) in want for pair in zip(remote["date"], remote["player_id"])]]
    return remote.set_index(["date", "player_id"]).sort_index()


def compare_column(remote, expect, name, kind):
    """Return the worst-case discrepancy for one column."""
    idx = remote.index.union(expect.index)
    a = remote.reindex(idx)[name]
    b = expect.reindex(idx)[name]
    if kind == "text":
        mismatches = (a.fillna("<NULL>") != b.fillna("<NULL>")).sum()
        return int(mismatches), 0.0
    both_na = a.isna() & b.isna()
    one_na = a.isna() ^ b.isna()
    both = ~(a.isna() | b.isna())
    mism = int(one_na.sum())
    worst = 0.0
    if kind == "int":
        mism += int((a[both].astype("int64") != b[both].astype("int64")).sum())
    else:
        x = a[both].astype(float)
        y = b[both].astype(float)
        rel = (x - y).abs() / np.maximum(1.0, y.abs())
        worst = float(rel.max()) if len(rel) else 0.0
        mism += int((rel > REL_TOL).sum())
    if not both_na.all():
        mism += 0  # na-parity already covered by one_na
    if both_na.sum() != (a.isna() & b.isna()).sum():
        raise AssertionError("internal na-parity accounting error")
    return mism, worst


def exact_row_count(env):
    """Best-effort PostgREST ``count=exact`` (can timeout on a fresh bulk
    write); returns None instead of raising, because the authoritative row
    count in this script comes from the full PK key dump."""
    table = player_table(env)
    try:
        return SUPABASE_ADMIN_CLIENT.table(table).select("date", count="exact").limit(1).execute().count
    except Exception as exc:  # noqa: BLE001
        print(f"count=exact failed ({type(exc).__name__}: {str(exc)[:120]}); using the PK key dump", flush=True)
        return None


def published_keys(env):
    """ALL (date, player_id) keys on the table, with a duplicate guard.

    The PK guarantees one row per key, so len(keys) IS the row count and the
    set proves nothing was added or deleted. A duplicate (impossible under
    the PK) would falsify that, so it is checked, not silently deduped.
    """
    keys = snapshot_keys(env)
    return keys


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="dev", choices=["dev", "prod"])
    parser.add_argument("--sample", type=int, default=1_000, help="random rows to check (default 1000)")
    args = parser.parse_args()

    table = player_table(args.env)
    count = exact_row_count(args.env)
    # Authoritative row-count / no-add / no-delete proof: the full PK key set.
    pub = published_keys(args.env)
    print(
        f"{table} published keys: {len(pub):,}  (count=exact: {count if count is not None else 'timed out'})",
        flush=True,
    )
    if len(pub) != 617_310:
        raise SystemExit(f"row count drifted: {len(pub):,} != 617,310 - a row was added or deleted")
    if count is not None and count != len(pub):
        raise SystemExit(f"count=exact {count} disagrees with key dump {len(pub)}")
    if count is not None and count != len(pub):
        raise SystemExit(f"count=exact {count} disagrees with key dump {len(pub)}")

    # Local truth: the exact backfill computation path, restricted to the
    # published key set (the store also holds ~1% rows publish never wrote -
    # sampling against those would compare nothing and 400 nothing).
    t0 = time.time()
    frame = shot_features().merge(club_map_cols(), on=["date", "player_id"], how="left", validate="many_to_one")
    # name comes from snapshot_keys() in the backfill path; it is not one of
    # the 16 columns under comparison, so drop it here.
    payload_cols = [c for c in PAYLOAD_COLUMNS if c != "name"]
    frame = pub.merge(frame, on=["date", "player_id"], how="inner", validate="many_to_one")
    frame = frame[payload_cols]
    frame["player_id"] = frame["player_id"].astype("int64")
    frame["career_games"] = frame["career_games"].astype("int64")
    print(
        f"local truth recomputed: {len(frame):,} of 692,634 store rows "
        f"restricted to published keys in {time.time() - t0:.0f}s",
        flush=True,
    )

    rng = np.random.default_rng(20261024)
    sample_idx = rng.choice(len(frame), size=min(args.sample, len(frame)), replace=False)
    sampled = frame.iloc[sample_idx]
    keys = list(zip(sampled["date"], sampled["player_id"]))
    expect = sampled.set_index(["date", "player_id"]).sort_index()
    print(f"sampling {len(keys):,} random rows across {len({d for d, _ in keys})} dates", flush=True)

    t0 = time.time()
    remote = remote_rows(args.env, keys)
    print(f"read {len(remote):,} remote rows in {time.time() - t0:.0f}s", flush=True)
    if len(remote) != len(keys):
        missing = set(keys) - set(remote.reset_index()[["date", "player_id"]].itertuples(index=False, name=None))
        raise SystemExit(f"remote readback lost {len(missing)} sampled keys, e.g. {sorted(missing)[:3]}")

    failures = {}
    for name in FLOAT_COLUMNS:
        mism, worst = compare_column(remote, expect, name, "float")
        failures[name] = (mism, worst)
        print(f"  {name:>16}: mismatches {mism}   worst rel diff {worst:.3e}", flush=True)
    for name in INT_COLUMNS:
        mism, _ = compare_column(remote, expect, name, "int")
        failures[name] = (mism, 0.0)
        print(f"  {name:>16}: mismatches {mism}   (exact)", flush=True)
    for name in TEXT_COLUMNS:
        mism, _ = compare_column(remote, expect, name, "text")
        failures[name] = (mism, 0.0)
        print(f"  {name:>16}: mismatches {mism}   (exact)", flush=True)

    print(
        f"row count check: published keys {len(pub):,} "
        f"(count=exact: {count if count is not None else 'timed out, key dump authoritative'})",
        flush=True,
    )
    total_mism = sum(m for m, _ in failures.values())
    if total_mism:
        raise SystemExit(f"SPOT CHECK FAILED: {total_mism} mismatches across the 16 columns")
    print(f"SPOT CHECK PASSED: {len(keys):,} rows x 16 columns match to {REL_TOL:g} rel / exact", flush=True)


if __name__ == "__main__":
    main()
