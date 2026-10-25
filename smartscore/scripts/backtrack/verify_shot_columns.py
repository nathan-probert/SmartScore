"""Acceptance check for the shot-columns migration (docs/supabase_shot_columns.md §6).

Runs the V16 base arm (ensemble, k=5 date k-fold) twice per seed - once with
the sqlite frame path (shot_features()/club_map() over data/raw_nhl.sqlite)
and once with the supabase frame path (the columns the one-shot backfill
published to Player-Snapshots-backtrack-dev) - and compares the pooled
ensemble picks. The migration is correct when both paths produce the same
hits/total within 0.1 percentage points (they should normally be bit-identical;
the every-row pick comparison catches even that gap).

No feature math is forked here: frames go through model.build_frame /
model.add_derived with the same run_folds protocol exp_exposure.py's base arm
uses, driven by exp_exposure.run_arm (identical args namespace, lightgbm import
handled by `uv run --with lightgbm==4.7.0` - set OMP_NUM_THREADS=2
LGBM_N_JOBS=2 like the notes' runbooks).

Usage::

    OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \\
      smartscore/scripts/backtrack/verify_shot_columns.py --env dev --seeds 1 3
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[2]))
sys.path.append(str(Path(__file__).resolve().parent))

import exp_exposure as exposure  # noqa: E402
import model as M  # noqa: E402


def run_source_arm(source, seed, env, log):
    """One base-arm run_folds on the frame built from `source`; returns pick tables."""
    players, teams, goalies = M.load_frames(env, refresh=False)  # noqa: E501 - long argv line docstring
    frame = M.build_frame(players, teams, goalies, source=source)
    frame = M.add_derived(frame, teams, source=source)
    log(
        f"\n[{source} seed {seed}] frame rows {len(frame):,}; shot coverage "
        f"{frame['sd_shots_pg'].notna().sum():,}; club coverage {frame['club'].notna().sum():,}"
    )

    stats, pooled, out_rows, aucs = exposure.run_arm(frame, seed, log, "base")
    picks_ensemble = pd.concat(pooled["ensemble"], ignore_index=True)
    row = M.score_picks(picks_ensemble, "ensemble")
    log(
        f"[{source} seed {seed}] pooled ensemble baseline {row['hits']}/{row['total']} = "
        f"{100 * row['hits'] / row['total']:.3f}% (mean AUC {np.mean(aucs):.4f})"
    )
    cols = ["date", "player_id", "name", "tims", "scored", "ensemble"]
    table = picks_ensemble[cols].assign(year=seed)
    return row, table, picks_ensemble


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 3])
    parser.add_argument("--sources", nargs="+", default=["sqlite", "supabase"], choices=["sqlite", "supabase"])
    # noqa lines: S301 = our own just-pickled local artifacts, not untrusted data
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[3] / "data" / "ml" / "verify_shot_columns.csv")  # noqa: E501
    parser.add_argument("--log-file", type=Path, default=None)
    args = parser.parse_args()

    log = exposure.Log(args.log_file or Path(__file__).resolve().parent / "verify_shot_columns.log")

    rows = []
    for source in args.sources:
        for seed in args.seeds:
            t0 = time.time()
            row, table, _ = run_source_arm(source, seed, args.env, log)
            table.to_pickle(args.out.parent / f"verify_shotcols_picks_{source}_seed{seed}.pkl")
            row.update({"source": source, "seed": seed, "wall_s": round(time.time() - t0)})
            rows.append(row)
    summary = pd.DataFrame(rows)[
        ["source", "seed", "hits", "total", "acc", "method", "hits_day", "days", "wall_s"]
    ]
    log("\n=== acceptance summary (pooled ensemble picks) ===")
    log(summary.to_string(index=False))

    for seed in args.seeds:
        if {"sqlite", "supabase"} <= set(args.sources):
            sql = pd.read_pickle(args.out.parent / f"verify_shotcols_picks_sqlite_seed{seed}.pkl")  # noqa: S301
            sup = pd.read_pickle(args.out.parent / f"verify_shotcols_picks_supabase_seed{seed}.pkl")  # noqa: S301
            if len(sql) != len(sup):
                log(f"seed {seed}: ROF pick table sizes differ {len(sql)} vs {len(sup)}")
                continue
            joined = sql.merge(sup, on=["date", "player_id"], suffixes=("_s", "_b"))
            same_picks = (joined["tims_s"] == joined["tims_b"]).all()
            score_gap = (joined["ensemble_s"].astype(float) - joined["ensemble_b"].astype(float)).abs().max()
            log(
                f"seed {seed}: {len(joined):,} pooled rows, identical pick set: {same_picks}, "
                f"max ensemble-score gap {score_gap:.10f}"
            )
    log.flush()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(args.out, index=False)
    log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
