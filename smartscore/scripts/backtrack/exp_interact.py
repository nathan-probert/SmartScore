"""V16-interact — three untried pairwise-interaction feature groups.

Everything here is pairwise interaction COLUMNS between features that already
exist in ``model.py``'s frame (no new standalone signals, no new joins, no
model.py edits). Three declared groups:

  A. home x player-form        `home` (~0/1) times the pre-game player-form
                               rates/percentiles.
  B. rest x lineup-role        per-player / per-club rest days split by the
                               pre-game ``lineup_position_group`` (F vs D),
                               i.e. role-conditional rest columns.
  C. opponent-strength rel.    player value times opponent strength
                               (opposing goalie GAA / save pct, opposing club
                               wins) and terms RELATIVE to the average
                               opponent of that date (same-date pre-game
                               metadata, the V3b construct).

Not a redo of V12-interactions (tims x rates — rejected): those interacted the
tims GROUP INDEX with rates; these interact home/rest/role/opponent-strength
with them, and no tims product appears.

All interaction inputs are pre-game: home, gpg/hgpg/five_gpg/ppg and the
rank_* percentiles are the row's own snapshot values; prev2_goals/ewm_goals
are shifted-per-player prior games; rest columns are prior dates;
lineup_position_group is the static roster position (allowed, unlike the
same-game shift-derived lineup_unit/pp_unit); opponent strength comes from the
opposing goalie row and the opposing club record joined on team abbreviation.

Protocol (never relaxed): date-blocked k-fold, k=5 over the 1,053 challenge
dates, --train-subset all, one pass of model.run_folds with
--model ensemble, which yields the lgbm AND ensemble arms plus in-run gpg /
hgpg baselines on identical folds. Pooled OOF hits/total, per-fold
mean +/- std, per-group, 2023-24+ and 2025-26 subsets, exact McNemar vs gpg,
hgpg and vs the corrected no-interaction reference OOFs (oof_v6_baseline /
oof_v6c_ensemble, same seed) on pooled paired picks.

Usage:
  OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
      smartscore/scripts/backtrack/exp_interact.py --env dev --seed 1
"""

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.append(os.path.dirname(__file__))

import model as M  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "ml"

# Declared interaction columns; names end in _i in the "interacted" position,
# none of them end with _x/_y so merge-suffix guards elsewhere still work.
HOME_FORM_COLUMNS = [
    "home_x_gpg",
    "home_x_hgpg",
    "home_x_five_gpg",
    "home_x_ppg",
    "home_x_rank_gpg",
    "home_x_rank_hgpg",
    "home_x_ewm_goals",
    "home_x_prev2_goals",
]

REST_ROLE_COLUMNS = [
    "club_rest_days_F",
    "club_rest_days_D",
    "days_since_game_F",
    "days_since_game_D",
]

OPP_REL_COLUMNS = [
    "gpg_x_opp_gaa",
    "hgpg_x_opp_gaa",
    "five_gpg_x_opp_gaa",
    "gpg_x_opp_save_pct",
    "hgpg_x_opp_save_pct",
    "gpg_x_rel_opp_gaa",
    "hgpg_x_rel_opp_gaa",
    "gpg_x_opp_wins",
]

INTERACTION_COLUMNS = HOME_FORM_COLUMNS + REST_ROLE_COLUMNS + OPP_REL_COLUMNS


def add_interactions(frame):
    """Add the three interaction groups in place (no joins, no row changes).

    Every input column already exists on the frame from build_frame /
    add_derived. NaN inputs propagate to NaN (HGB splits on NaN natively);
    home=0 rows keep 0*value=0, except where the value itself is NaN.
    """
    n_rows = len(frame)
    frame = frame.copy()

    h = frame["home"].astype(float)
    for col in ("gpg", "hgpg", "five_gpg", "ppg", "rank_gpg", "rank_hgpg",
                "ewm_goals", "prev2_goals"):
        frame[f"home_x_{col}"] = h * pd.to_numeric(frame[col], errors="coerce")

    pos = frame["lineup_position_group"].astype("string")
    for role in ("F", "D"):
        frame[f"club_rest_days_{role}"] = frame["club_rest_days"].where(pos == role)
        frame[f"days_since_game_{role}"] = frame["days_since_game"].where(pos == role)

    for col in ("gpg", "hgpg", "five_gpg"):
        rate = pd.to_numeric(frame[col], errors="coerce")
        frame[f"{col}_x_opp_gaa"] = rate * pd.to_numeric(frame["opp_gaa"], errors="coerce")
        frame[f"{col}_x_opp_save_pct"] = rate * pd.to_numeric(frame["opp_save_pct"], errors="coerce")
    frame["gpg_x_opp_wins"] = pd.to_numeric(frame["gpg"], errors="coerce") * pd.to_numeric(
        frame["opp_wins"], errors="coerce"
    )

    # Opponent strength relative to the average opponent of that date. Only
    # same-date pre-game metadata enters (each distinct opponent's opposing
    # goalie row), nothing from the row's own game.
    per_opp = (
        frame[["date", "opp", "opp_gaa"]]
        .drop_duplicates(["date", "opp"])
        .dropna(subset=["opp_gaa"])
    )
    date_mean = per_opp.groupby("date")["opp_gaa"].mean().rename("date_mean_opp_gaa")
    frame = frame.merge(date_mean.reset_index(), on="date", how="left", validate="many_to_one")
    rel = pd.to_numeric(frame["opp_gaa"], errors="coerce") - frame["date_mean_opp_gaa"]
    for col in ("gpg", "hgpg"):
        frame[f"{col}_x_rel_opp_gaa"] = pd.to_numeric(frame[col], errors="coerce") * rel
    frame = frame.drop(columns="date_mean_opp_gaa")

    if len(frame) != n_rows:
        raise RuntimeError(f"add_interactions changed row count: {n_rows} -> {len(frame)}")
    missing = [col for col in INTERACTION_COLUMNS if col not in frame.columns]
    if missing:
        raise RuntimeError(f"interaction columns missing after derivation: {missing}")
    suffixes = [col for col in frame.columns if col.endswith(("_x", "_y"))]
    if suffixes:
        raise RuntimeError(f"merge suffix columns appeared: {suffixes}")
    return frame


def verify_interactions(frame):
    """Coverage + independent recomputation spot checks."""
    print("\n=== interaction coverage (non-null / rows) ===")
    for col in INTERACTION_COLUMNS:
        nn = int(frame[col].notna().sum())
        print(f"  {col:24s} {nn:,} ({100 * nn / len(frame):.1f}%)")
    challenge = frame["tims"].isin([1, 2, 3]) & frame["scored"].notna()
    n_ch = int(challenge.sum())
    print(f"\n=== interaction coverage on challenge rows ({n_ch:,}) ===")
    for col in INTERACTION_COLUMNS:
        nn = int(frame.loc[challenge, col].notna().sum())
        print(f"  {col:24s} {nn:,}/{n_ch:,} ({100 * nn / n_ch:.1f}%)")

    rng = np.random.default_rng(7)
    idx = rng.choice(len(frame), size=4, replace=False)
    print("\n=== spot checks (independent recompute) ===")
    for i in idx:
        row = frame.iloc[i]
        pos = row["lineup_position_group"]
        checks = {
            "home_x_gpg": float(row["home"]) * (float(row["gpg"]) if pd.notna(row["gpg"]) else np.nan),
            "home_x_ewm_goals": float(row["home"]) * (float(row["ewm_goals"]) if pd.notna(row["ewm_goals"]) else np.nan),
            "club_rest_days_F": float(row["club_rest_days"]) if pos == "F" else np.nan,
            "days_since_game_D": float(row["days_since_game"]) if pos == "D" else np.nan,
            "gpg_x_opp_gaa": (None if pd.isna(row["gpg"]) or pd.isna(row["opp_gaa"])
                              else float(row["gpg"]) * float(row["opp_gaa"])),
        }
        bad = []
        for name, expect in checks.items():
            got = row[name]
            ok = (pd.isna(expect) and pd.isna(got)) or (
                expect is not None and not pd.isna(expect) and abs(float(got) - expect) < 1e-9
            )
            if not ok:
                bad.append(f"{name}: got {got} expected {expect}")
        rel_ok = True
        if pd.notna(row["gpg_x_rel_opp_gaa"]):
            per_opp = (
                frame.loc[frame["date"] == row["date"], ["opp", "opp_gaa"]]
                .drop_duplicates("opp").dropna(subset=["opp_gaa"])
            )
            rel = float(row["opp_gaa"]) - per_opp["opp_gaa"].mean()
            rel_ok = abs(float(row["gpg_x_rel_opp_gaa"]) - float(row["gpg"]) * rel) < 1e-9
        status = "OK" if (not bad and rel_ok) else f"MISMATCH {bad} rel_ok={rel_ok}"
        print(
            f"  row {i} ({row['date']} player {row['player_id']}): {status}"
        )
        if bad or not rel_ok:
            raise RuntimeError("interaction spot check failed")


# --- evaluation helpers (shared shape with exp_group3) ---


def re_pick(oof_path, score_col):
    """Pooled picks from a saved baseline OOF file, via model.daily_picks."""
    oof = pd.read_csv(oof_path)
    oof["tims"] = pd.to_numeric(oof["tims"], errors="coerce").astype("Int64")
    return M.daily_picks(oof, score_col)


def paired_counts(variant_picks, baseline_picks):
    """McNemar exact counts + p on pooled picks paired by (date, tims)."""
    from scipy.stats import binomtest

    key = ["date", "tims"]
    m = variant_picks[key + ["scored"]].merge(
        baseline_picks[key + ["scored"]], on=key, suffixes=("_v", "_b")
    )
    var_hit = m["scored_v"] == 1
    base_hit = m["scored_b"] == 1
    a = int((var_hit & ~base_hit).sum())
    b = int((~var_hit & base_hit).sum())
    n = a + b
    p = binomtest(a, n, 0.5).pvalue if n else 1.0
    return a, b, p, len(m)


def changed_picks(variant_picks, baseline_picks):
    key = ["date", "tims"]
    m = variant_picks[key + ["player_id"]].merge(
        baseline_picks[key + ["player_id"]], on=key, suffixes=("_v", "_b")
    )
    return int((m["player_id_v"] != m["player_id_b"]).sum()), len(m)


def per_group(picks):
    groups = picks.groupby("tims")["scored"].agg(["count", "sum"])
    return " ".join(
        f"g{int(t)}={100 * row['sum'] / row['count']:.1f}({int(row['sum'])}/{int(row['count'])})"
        for t, row in groups.iterrows()
    )


def era_subset(picks, start):
    sub = picks[picks["date"] >= start]
    hits = int((sub["scored"] == 1).sum())
    return f"{hits}/{len(sub)}={100 * hits / len(sub):.2f}%" if len(sub) else "n/a"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--splits", type=int, default=5)
    parser.add_argument("--lgbm-config", default="a")
    parser.add_argument("--smoke", action="store_true", help="first ~120 challenge dates only")
    parser.add_argument("--oof-file", type=Path, default=None)
    args = parser.parse_args()

    t0 = time.time()
    players, teams, goalies = M.load_frames(args.env)
    frame = M.build_frame(players, teams, goalies)
    frame = M.add_derived(frame, teams)
    frame = add_interactions(frame)
    verify_interactions(frame)
    print(f"\nframe built: {len(frame):,} rows in {time.time() - t0:.0f}s", flush=True)

    # Patch the feature lists model.run_folds consumes. Careful: model.py's
    # main() does its own patching for --lineup; we bypass main() entirely.
    M.NUMERIC_FEATURES = list(M.NUMERIC_FEATURES) + INTERACTION_COLUMNS
    M.DATASET_FEATURES = M.NUMERIC_FEATURES + list(M.CATEGORICAL_FEATURES)
    print(f"features: {len(M.NUMERIC_FEATURES)} numeric + {len(M.CATEGORICAL_FEATURES)} categorical "
          f"(+{len(INTERACTION_COLUMNS)} interactions)")

    challenge = frame["tims"].isin([1, 2, 3]) & frame["scored"].notna()
    challenge_dates = frame.loc[challenge, "date"].unique()
    if args.smoke:
        challenge_dates = sorted(challenge_dates)[:120]
    run_args = argparse.Namespace(
        split="kfold",
        splits=args.splits,
        seed=args.seed,
        train_subset="all",
        model="ensemble",
        lgbm_config=args.lgbm_config,
        ranker="none",
        model_file=None,
    )
    oof_path = args.oof_file or (CACHE_DIR / f"oof_v16interact_seed{args.seed}.csv")
    if not args.smoke:
        run_args.model_file = oof_path

    stats, pooled, out_rows, aucs = M.run_folds(frame, run_args)

    pooled_picks = {method: pd.concat(parts, ignore_index=True) for method, parts in pooled.items()}
    per_fold = pd.DataFrame(stats).pivot(index="fold", columns="method", values="acc")

    tag = f"seed {args.seed}" + (" SMOKE" if args.smoke else "")
    print(f"\n=== pooled out-of-fold picks ({tag}) ===")
    print("AUC per fold:", ", ".join(f"{a:.4f}" for a in aucs),
          f"(mean {np.mean(aucs):.4f} +- {np.std(aucs):.4f})")
    for method in ("lgbm", "ensemble", "hgb", "logreg", "hgpg", "gpg"):
        picks = pooled_picks[method]
        hits = int((picks["scored"] == 1).sum())
        total = len(picks)
        acc_mean = per_fold[method].mean()
        acc_std = per_fold[method].std()
        print(
            f"{method:9s} {hits}/{total} = {100 * hits / total:.3f}% "
            f"(folds {acc_mean:.3f} +- {acc_std:.3f}) | {per_group(picks)} | "
            f"2023-24+ {era_subset(picks, '2023-08-01')} | 2025-26 {era_subset(picks, '2025-08-01')}"
        )

    print("\n=== exact McNemar (pooled paired picks) ===")
    # In-run baselines on identical folds.
    for arm in ("lgbm", "ensemble"):
        for ref in ("hgpg", "gpg"):
            a, b, p, n = paired_counts(pooled_picks[arm], pooled_picks[ref])
            ch, _ = changed_picks(pooled_picks[arm], pooled_picks[ref])
            print(f"{arm:9s} vs {ref:5s}: {a}-{b}, p={p:.4f} (n={n}, changed picks={ch})")

    # Reference no-interaction OOFs from the same seed (corrected baseline).
    if args.smoke:
        print("(smoke: reference-OOF pairing skipped)")
    else:
        # Same-frame no-interaction reference preferred: the calibration
        # worker's model.py --model ensemble repro on this HEAD (617,310-row
        # V15-augmented cache, same --seed). Fall back to the V6-era 427k OOFs
        # only if the same-frame file is missing.
        ref_path = CACHE_DIR / f"oof_v16_calib_ens_seed{args.seed}.csv"
        if ref_path.exists():
            ref_oof = pd.read_csv(ref_path)
            ref_tag = f"same-frame ({ref_path.name})"
            m = out_rows[["date", "player_id", "fold"]].merge(
                ref_oof[["date", "player_id", "fold"]].rename(columns={"fold": "fold_ref"}),
                on=["date", "player_id"],
                validate="one_to_one",
            )
            mismatch = int((m["fold"] != m["fold_ref"].astype("Int64")).sum())
            print(f"reference = {ref_tag}; fold mismatch: {mismatch}/{len(m)}")
            if mismatch:
                raise RuntimeError("reference OOF folds do not match this run's fold assignment")
            ref_lgbm = M.daily_picks(ref_oof, "lgbm")
            ref_ens = M.daily_picks(ref_oof, "ensemble")
        else:
            ref_tag = "v6-era (427k frame) OOFs"
            ref_lgbm = re_pick(CACHE_DIR / f"oof_v6_baseline_seed{args.seed}.csv", "lgbm")
            ref_ens = re_pick(CACHE_DIR / f"oof_v6c_ensemble_seed{args.seed}.csv", "ensemble")
        rl_hits = int((ref_lgbm["scored"] == 1).sum())
        re_hits = int((ref_ens["scored"] == 1).sum())
        print(f"reference re-picks ({ref_tag}): lgbm {rl_hits}/{len(ref_lgbm)}, ensemble {re_hits}/{len(ref_ens)}")
        for arm, ref, ref_hits, ref_total in (
            ("lgbm", ref_lgbm, rl_hits, len(ref_lgbm)),
            ("ensemble", ref_ens, re_hits, len(ref_ens)),
        ):
            our_hits = int((pooled_picks[arm]["scored"] == 1).sum())
            delta = 100 * our_hits / len(pooled_picks[arm]) - 100 * ref_hits / ref_total
            a, b, p, n = paired_counts(pooled_picks[arm], ref)
            ch, _ = changed_picks(pooled_picks[arm], ref)
            print(f"{arm:9s} vs no-interact ref: {a}-{b}, p={p:.4f} "
                  f"(delta {delta:+.3f}pp, n={n}, changed picks={ch})")

    if not args.smoke:
        out_rows.to_csv(oof_path, index=False)
        print(f"\nsaved {len(out_rows):,} OOF rows -> {oof_path}")
    print(f"total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
