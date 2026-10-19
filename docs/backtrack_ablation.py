"""Supplementary ablation: LGBM with the same-game lookahead features removed.

`lineup_unit` and `pp_unit` are reconstructed from the same game's shift charts
(`lineup_status=ACTUAL`), so they are lookahead at pick time. This keeps the
pre-game roster classification (`lineup_position_group`) and `tims`, drops the
two post-game columns, and reruns the exact date-blocked fold evaluation.

`--drop-goalie` additionally removes the opponent-goalie block
(`opp_gaa`, `opp_save_pct`, `opp_shutouts`, `opp_games_played`, `opp_wins`,
`opp_losses`, `opp_otl`). Those rate values are pre-game, but the identity of
the goalie is chosen from actual appearances, so the block is not fully pre-game.

Usage:
    OMP_NUM_THREADS=2 uv run --no-sync python docs/backtrack_ablation.py
    OMP_NUM_THREADS=2 uv run --no-sync python docs/backtrack_ablation.py --drop-goalie
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "smartscore" / "scripts" / "backtrack"))
import model as M  # noqa: E402

OPP_GOALIE_FEATURES = [
    "opp_gaa",
    "opp_save_pct",
    "opp_shutouts",
    "opp_games_played",
    "opp_wins",
    "opp_losses",
    "opp_otl",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--drop-goalie",
        action="store_true",
        help="also remove the opponent-goalie block (actual-appearance identity lookahead)",
    )
    cli = parser.parse_args()

    numeric = M.NUMERIC_FEATURES
    if cli.drop_goalie:
        numeric = [col for col in numeric if col not in OPP_GOALIE_FEATURES]
    # Pre-game-only feature set: drop lineup_unit / pp_unit (same-game actuals).
    M.DATASET_FEATURES = numeric + ["lineup_position_group", "tims"]
    M.CATEGORICAL_FEATURES = ["lineup_position_group", "tims"]

    tag = "no-lineup+no-goalie" if cli.drop_goalie else "no-lineup"
    stem = "audit_lgbm_pregame.csv" if cli.drop_goalie else "audit_lgbm_nolineup.csv"
    model_file = Path("/tmp/opencode") / stem  # noqa: S108 - audit scratch path

    args = argparse.Namespace(
        split="kfold",
        splits=5,
        seed=0,
        train_subset="all",
        model="lgbm",
        ranker="none",
        model_file=model_file,
    )

    players, teams, goalies = M.load_frames("dev")
    frame = M.add_derived(M.build_frame(players, teams, goalies), teams)
    stats, pooled, out_rows, aucs = M.run_folds(frame, args)

    # Independent pick rule (not model.daily_picks).
    picks = []
    for _, day in out_rows.groupby("date"):
        for tims in (1, 2, 3):
            members = day[day["tims"] == tims]
            finite = members[members["lgbm"].notna()]
            if not finite.empty:
                picks.append(finite.loc[finite["lgbm"].idxmax()])
    picks = pd.DataFrame(picks)
    print(
        f"lgbm {tag}: {int((picks['scored'] == 1).sum())}/{len(picks)} = "
        f"{100 * (picks['scored'] == 1).mean():.3f}%"
    )
    print(f"mean AUC {sum(aucs) / len(aucs):.4f}")
    out_rows.to_csv(args.model_file, index=False)


if __name__ == "__main__":
    main()
