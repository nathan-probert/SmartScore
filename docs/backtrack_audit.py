"""Standalone audit of the SmartScore backtrack pick evaluation.

Deliberately does NOT import ``model.py`` (or its ``daily_picks`` /
``score_picks`` path). Reads the cached Supabase CSV directly and applies the
published rule:

    one pick per (date, tims in {1,2,3}) = argmax of the metric among the
    challenge rows; a hit iff that row's ``scored`` == 1;
    denominator = 3 * number of challenge dates.

With ``--oof`` it also verifies the fold structure of a saved out-of-fold CSV:
every date is held out exactly once, the deterministic date blocks reproduce
the recorded ``fold`` column, and each fold's training dates are the complement
of its holdout dates (therefore disjoint).

Usage:
    uv run --no-sync python docs/backtrack_audit.py
    uv run --no-sync python docs/backtrack_audit.py --oof /tmp/opencode/audit_lgbm_oof.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "ml"


def daily_argmax(frame: pd.DataFrame, col: str) -> pd.DataFrame:
    """One row per (date, tims) with the largest ``col``; ties keep file order."""
    picks = []
    for _, group in frame.groupby(["date", "tims"], sort=True):
        finite = group[group[col].notna()]
        if finite.empty:
            continue
        picks.append(finite.loc[finite[col].idxmax()])
    return pd.DataFrame(picks)


def baseline_report(players: pd.DataFrame) -> pd.DataFrame:
    challenge = players[players["tims"].isin([1, 2, 3]) & players["scored"].notna()].copy()
    dates = challenge["date"].nunique()
    groups = challenge.groupby(["date", "tims"]).size()
    print("=== challenge frame ===")
    print(f"rows={len(challenge):,}  dates={dates}  expected picks=3*{dates}={3 * dates}")
    print(
        f"groups={len(groups)}  full grid={len(groups) == 3 * dates}  "
        f"group size min/max={groups.min()}/{groups.max()}"
    )

    rows = []
    for col in ("gpg", "hgpg"):
        picks = daily_argmax(challenge, col)
        hits = int((picks["scored"] == 1).sum())
        rows.append(
            {
                "method": col,
                "hits": hits,
                "total": len(picks),
                "acc": 100 * hits / len(picks),
                "dates": picks["date"].nunique(),
                "per_date_3": int((picks.groupby("date").size() == 3).sum()),  # noqa: PLR2004
            }
        )
        n3 = int((picks.groupby("date").size() == 3).sum())  # noqa: PLR2004
        print(
            f"{col:5s} hits={hits:4d}/{len(picks)} = {100 * hits / len(picks):.3f}%  "
            f"dates={picks['date'].nunique()}  dates_with_3_picks={n3}"
        )

    # A group whose every score is NaN would silently fall back to the first
    # row in the harness (the baselines fill NaN with -inf before picking).
    all_nan = challenge.groupby(["date", "tims"])["gpg"].apply(lambda s: s.isna().all()).sum()
    all_nan_h = challenge.groupby(["date", "tims"])["hgpg"].apply(lambda s: s.isna().all()).sum()
    print(f"all-NaN groups: gpg={all_nan} hgpg={all_nan_h}")

    # Ties at the maximum: idxmax takes the first row, so record how common.
    for col in ("gpg", "hgpg"):
        tied = 0
        for _, group in challenge.groupby(["date", "tims"]):
            top = group[col].max()
            if (group[col] == top).sum() > 1:
                tied += 1
        print(f"{col}: groups with a tied maximum={tied}")
    return pd.DataFrame(rows)


def fold_report(oof: pd.DataFrame, splits: int, seed: int) -> None:
    print("\n=== fold structure ===")
    dates = np.array(sorted(oof["date"].unique()))
    rng = np.random.default_rng(seed)
    shuffled = dates.copy()
    rng.shuffle(shuffled)
    blocks = np.array_split(shuffled, splits)

    ok_fold = True
    seen: dict[str, int] = {}
    for number, block in enumerate(blocks, start=1):
        block_set = set(block)
        got = set(oof.loc[oof["fold"] == number, "date"].unique())
        if got != block_set:
            ok_fold = False
            print(f"fold {number}: MISMATCH got={len(got)} expected={len(block_set)}")
        for date in block_set:
            seen[date] = seen.get(date, 0) + 1

    all_dates = set(dates)
    print(f"dates={len(dates)}  each held out exactly once={all(d == 1 for d in seen.values())}")
    print(f"recorded fold column reproduces seed={seed} date blocks={ok_fold}")
    for number, block in enumerate(blocks, start=1):
        holdout = set(block)
        train = all_dates - holdout
        overlap = holdout & train
        print(f"fold {number}: holdout={len(holdout):4d} train={len(train):4d} date overlap={len(overlap)}")
        assert not overlap  # noqa: S101 - audit invariant

    print("\n=== OOF pick accounting ===")
    labels = {"date", "player_id", "name", "team_name", "home", "tims", "scored", "fold"}
    for col in [c for c in oof.columns if c not in labels]:
        picks = daily_argmax(oof, col)
        hits = int((picks["scored"] == 1).sum())
        print(f"{col:10s} hits={hits:4d}/{len(picks)} = {100 * hits / max(len(picks), 1):.3f}%")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--oof", type=Path, help="saved out-of-fold CSV to audit")
    parser.add_argument("--splits", type=int, default=5)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    players = pd.read_csv(CACHE / "player_snapshots_backtrack_dev.csv")
    baseline_report(players)
    if args.oof:
        fold_report(pd.read_csv(args.oof), args.splits, args.seed)


if __name__ == "__main__":
    main()
