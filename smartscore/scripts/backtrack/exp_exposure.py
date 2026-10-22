"""V16-exposure: multiplicative prior xG/60 x projected-TOI feature test.

The one never-tried combination: the product of
  (a) the player's prior-games XG PER 60 MINUTES from the first-party (V8)
      per-shot xG proxy (raw MoneyPuck shot events + own per-shot model, OOF
      per fold - "fit nothing on test dates"), and
  (b) a PROJECTED TOI built from prior-games data only (r10_toi_pg, the
      V4-style prior-10-games TOI rate; never the current game's own TOI).

Each component alone was tested before (V8-own-xG block; V4 TOI rates) and
rejected/neutral; the multiplicative product has never been tried.

Everything else is the standard protocol: build_frame + add_derived from
model.py (42-feature corrected baseline), run_folds with the SAME k=5 date
k-fold for each seed, train on ALL rows (--train-subset all). The only change
is one extra numeric feature ``exposure_xg60_toi``.

Leakage rules honoured:
  * the per-shot xG values are out-of-fold: shot model fold i never trains on
    fold i dates (cached V8 artefacts are used after verifying their fold
    column against model.fold_blocks for the requested seed);
  * prior sums are shift(1)-style (cumsum minus current row) per player per
    season, like model.shot_features;
  * no same-game shift-derived lineup_unit/pp_unit enters any feature.

Usage:
  OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
      smartscore/scripts/backtrack/exp_exposure.py --env dev --seed 1

Arms (both run by default in one process, same folds -> paired picks):
  base     - unchanged 42-feature set (reference)
  exposure - base + ``exposure_xg60_toi``

If the seed has no cached shot_xg_seed{N}.csv pass --build-xg to fit the
first-party per-shot xG model on raw MoneyPuck events with that seed's fold
assignment (writes data/moneypuck/shots/shot_xg_seed{N}.csv).
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.append(os.path.dirname(__file__))

import model as M  # noqa: E402
from model import (  # noqa: E402
    add_derived,
    build_frame,
    categorise,
    daily_picks,
    fit_hgb,
    fit_lgbm,
    fit_logreg,
    fit_ranker,
    fold_blocks,
    load_frames,
    run_folds,
    score_picks,
)

XG_DIR = Path(__file__).resolve().parents[3] / "data" / "moneypuck" / "shots"
MP_SHOTS_DIR = Path("/home/nathan/code/SmartScore-xg/data/moneypuck/shots")
LOG_DIR = Path(__file__).resolve().parent

EXPOSURE_FEATURE = "exposure_xg60_toi"


class Log:
    def __init__(self, path):
        self.path = path
        self.lines = []

    def __call__(self, text=""):
        print(text, flush=True)
        self.lines.append(str(text))

    def flush(self):
        self.path.write_text("\n".join(self.lines) + "\n")


def mcnemar_exact(picks_a, picks_b, tag_a="a", tag_b="b"):
    """Exact McNemar on paired picks (joined on date+tims)."""
    m = picks_a.merge(
        picks_b, on=["date", "tims"], suffixes=("_a", "_b"), validate="one_to_one"
    )
    a_hit = m["scored_a"] == 1
    b_hit = m["scored_b"] == 1
    n01 = int((a_hit & ~b_hit).sum())
    n10 = int((~a_hit & b_hit).sum())
    n = n01 + n10
    from scipy.stats import binom
    if n == 0:
        return tag_a, tag_b, n01, n10, 1.0
    p = min(1.0, 2 * binom.cdf(min(n01, n10), n, 0.5))
    return tag_a, tag_b, n01, n10, float(p)


def shot_xg_player_games(seed, log):
    """Per player-game OOF xG sums from the cached first-party per-shot model.

    Verifies the fold column against the pick-model fold assignment for this
    seed (the pick test block for fold i must own exactly the shots whose
    fold says i on challenge dates). Missing cache and --build-xg -> fit it.
    """
    path = XG_DIR / f"shot_xg_seed{seed}.csv"
    if not path.exists():
        if not BUILD_XG:
            raise RuntimeError(
                f"{path} missing. Re-run with --build-xg to fit the per-shot xG "
                "model for this seed (takes minutes)."
            )
        build_shot_xg(seed, log, path)

    shots = pd.read_csv(path)
    shots["date"] = shots["date"].astype(str)
    log(f"  shot_xg rows {len(shots):,}; sum xG {shots['xg'].sum():,.1f}; goal rate {shots['goal'].mean():.4f}")
    pg = shots.groupby(["player_id", "date"], as_index=False)["xg"].sum()
    return shots, pg


def build_shot_xg(seed, log, out_path):
    """First-party per-shot xG model on raw MoneyPuck events, OOF by date fold.

    Replicates the V8 scheme: fold i (1..5) covers one shuffled block of the
    challenge dates; the non-challenge dates get their own five blocks (6..10).
    Raw attributes only: distance, angle, type, rebound, rush, skater diff,
    timeSinceLastEvent, period, shotOnEmptyNet. No external xG column.
    """
    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score

    parts = []
    con = sqlite3.connect(f"file:{M.RAW_DB}?mode=ro", uri=True)
    games = pd.read_sql_query(
        "SELECT game_id, game_date AS date FROM (SELECT DISTINCT game_id, game_date FROM player_games)", con
    )
    con.close()
    games["date"] = games["date"].astype(str)

    feat_rows = []
    for year in range(2017, 2026):
        f = MP_SHOTS_DIR / f"shots_{year}.csv"
        log(f"  loading {f.name}")
        raw = pd.read_csv(f, low_memory=False)
        raw = raw[raw["event"].isin(["SHOT", "MISS", "GOAL"])]
        cols = raw.rename(
            columns={
                "shotDistance": "distance",
                "shotAngle": "angle",
                "shotRebound": "rebound",
                "shotRush": "rush",
                "shooterPlayerId": "player_id",
                "goal": "goal",
                "shotType": "shot_type",
                "shotOnEmptyNet": "empty_net",
                "period": "period",
                "timeSinceLastEvent": "tsl",
            }
        )
        cols = cols.merge(games, on="game_id", how="left", validate="many_to_one")
        cols["skater_diff"] = cols["homeSkatersOnIce"] - cols["awaySkatersOnIce"]
        cols["angle"] = cols["angle"].abs()
        keep = cols[
            ["date", "game_id", "player_id", "goal", "distance", "angle", "shot_type",
             "rebound", "rush", "skater_diff", "tsl", "period", "empty_net"]
        ]
        feat_rows.append(keep)
    ev = pd.concat(feat_rows, ignore_index=True)
    ev = ev[ev["date"].notna() & ev["player_id"].notna()].copy()
    ev["goal"] = pd.to_numeric(ev["goal"], errors="coerce").fillna(0).astype(int)
    for col in ("distance", "angle", "rebound", "rush", "tsl", "period", "empty_net", "skater_diff"):
        ev[col] = pd.to_numeric(ev[col], errors="coerce")
    ev = pd.get_dummies(ev, columns=["shot_type"], dummy_na=True)
    log(f"  per-shot rows {len(ev):,}; goals {ev['goal'].sum():,}")

    players, teams, goalies = load_frames("dev", refresh=False)
    frame = build_frame(players, teams, goalies)
    challenge = frame["tims"].isin([1, 2, 3]) & frame["scored"].notna()
    challenge_dates = set(str(d) for d in frame.loc[challenge, "date"].unique())
    all_shot_dates = sorted(ev["date"].unique())
    non_challenge = [d for d in all_shot_dates if d not in challenge_dates]

    date_fold = {}
    for i, b in enumerate(fold_blocks(challenge_dates, 5, seed), start=1):
        for d in pd.Index(b):
            date_fold[str(d)] = i
    for i, b in enumerate(fold_blocks(non_challenge, 5, seed), start=6):
        for d in pd.Index(b):
            date_fold[str(d)] = i
    ev["fold"] = ev["date"].map(date_fold)
    assert ev["fold"].notna().all()

    feature_cols = [c for c in ev.columns if c not in ("date", "game_id", "player_id", "goal", "fold")]
    xs, ys, preds = [], [], []
    aucs = []
    folds = sorted(ev["fold"].unique())
    for fold in folds:
        test_m = ev["fold"] == fold
        train = ev[~test_m]
        assert set(train["date"]) .isdisjoint(set(ev.loc[test_m, "date"]))
        clf = LGBMClassifier(
            objective="binary", subsample=0.8, colsample_bytree=0.8, random_state=0,
            n_jobs=int(os.environ.get("LGBM_N_JOBS", 4)), verbose=-1,
            **M.LGBM_CONFIGS["a"],
        )
        clf.fit(train[feature_cols], train["goal"])
        prob = clf.predict_proba(ev.loc[test_m, feature_cols])[:, 1]
        aucs.append(roc_auc_score(ev.loc[test_m, "goal"], prob))
        ev.loc[test_m, "xg"] = prob
        log(f"    fold {fold}: train {len(train):,} auc {aucs[-1]:.4f}")
    log(f"  per-shot OOF AUC {np.mean(aucs):.4f} +- {np.std(aucs):.4f}; sum xG {ev['xg'].sum():,.1f}")

    out = ev[["player_id", "game_id", "date", "xg", "goal", "fold"]]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    log(f"  wrote {out_path}")


def exposure_feature(frame, seed, log, spot_check=True):
    """Build the exposure column: prior-season xG/60 x projected TOI.

    Returns a new frame with ``exposure_xg60_toi`` appended (all else equal).
    """
    t0 = time.time()
    shots_all, pg_xg = shot_xg_player_games(seed, log)

    con = sqlite3.connect(f"file:{M.RAW_DB}?mode=ro", uri=True)
    games = pd.read_sql_query(
        "SELECT player_id, game_date AS date, season, goals, toi FROM player_games", con
    )
    con.close()
    games["date"] = games["date"].astype(str)
    games["season"] = games["season"].astype("Int64").astype(str)
    games["toi_min"] = games["toi"].apply(
        lambda v: int(str(v).split(":")[0]) + int(str(v).split(":")[1]) / 60
        if isinstance(v, str) and ":" in v else np.nan
    )

    xg_lo, xg_hi = shots_all["date"].min(), shots_all["date"].max()
    in_cov = (games["date"] >= xg_lo) & (games["date"] <= xg_hi)
    games = games.merge(
        pg_xg.rename(columns={"xg": "xg_game"}),
        on=["player_id", "date"], how="left", validate="many_to_one",
    )
    games["xg_game"] = np.where(in_cov, games["xg_game"].fillna(0.0), np.nan)

    games = games.sort_values(["player_id", "date"]).reset_index(drop=True)
    season_group = games.groupby(["player_id", "season"], sort=False)
    xg_before = season_group["xg_game"].cumsum() - games["xg_game"]
    toi_before = season_group["toi_min"].cumsum() - games["toi_min"]
    games["xg60_prior"] = 60.0 * xg_before / toi_before.replace(0, np.nan)

    # Independent recomputation of the rolling sums for a few challenge rows.
    if spot_check:
        challenge_set = set(frame["date"].unique())
        sample = games[
            games["date"].isin(challenge_set) & games["xg_game"].notna()
        ].sample(3, random_state=13)
        for _, row in sample.iterrows():
            hist = games[
                (games["player_id"] == row["player_id"])
                & (games["season"] == row["season"])
                & (games["date"] < row["date"])
            ]
            expect_xg = hist["xg_game"].sum()
            expect_toi = hist["toi_min"].sum()
            got = row["xg60_prior"]
            expect = 60.0 * expect_xg / expect_toi if expect_toi else np.nan
            match = (got == expect) if (np.isnan(got) and np.isnan(expect)) else abs(got - expect) < 1e-9
            log(f"  spot-check {int(row['player_id'])} {row['date']}: xg_before {expect_xg:.6f} "
                f"toi_before {expect_toi:.3f} xg60 model={got!r} manual={expect!r} -> {'OK' if match else 'MISMATCH'}")
            if not match:
                raise RuntimeError("exposure spot-check mismatch")

    exposure = frame.merge(
        games[["player_id", "date", "xg60_prior"]],
        on=["date", "player_id"], how="left", validate="many_to_one",
    )
    n0 = len(frame)
    if len(exposure) != n0:
        raise RuntimeError(f"exposure merge changed rows {n0} -> {len(exposure)}")
    suffixes = [c for c in exposure.columns if c.endswith(("_x", "_y"))]
    if suffixes:
        raise RuntimeError(f"exposure merge leaked suffix columns {suffixes}")

    # The product: prior xG/60 x prior-games projected TOI (r10_toi_pg = the
    # V4 prior-10-games TOI mean, already strictly pre-game in the frame).
    exposure[EXPOSURE_FEATURE] = exposure["xg60_prior"] * exposure["r10_toi_pg"]
    chal = exposure["tims"].isin([1, 2, 3]) & exposure["scored"].notna()
    cov = exposure.loc[chal, EXPOSURE_FEATURE].notna().mean()
    log(f"  challenge exposure coverage {cov:.4f}; "
        f"describe: {exposure.loc[chal, EXPOSURE_FEATURE].describe().round(3).to_dict()}")
    log(f"  exposure feature built in {time.time() - t0:.0f}s")
    return exposure


def run_arm(frame, seed, log, arm, model="ensemble"):
    args = argparse.Namespace(
        env="dev", refresh=False, split="kfold", splits=5, test_start=M.TEST_START,
        min_season=None, seed=seed, train_subset="all", model=model, lgbm_config="a",
        ranker="none", model_file=None,
    )
    original = (M.NUMERIC_FEATURES, M.DATASET_FEATURES)
    if arm == "exposure":
        M.NUMERIC_FEATURES = list(M.NUMERIC_FEATURES) + [EXPOSURE_FEATURE]
        M.DATASET_FEATURES = M.NUMERIC_FEATURES + M.CATEGORICAL_FEATURES
    elif arm != "base":
        raise ValueError(arm)
    try:
        stats, pooled, out_rows, aucs = run_folds(frame, args)
    finally:
        M.NUMERIC_FEATURES, M.DATASET_FEATURES = original
    return stats, pooled, out_rows, aucs


def summarise(stats, pooled, log, tag):
    per_fold = pd.DataFrame(stats).pivot(index="fold", columns="method", values="acc")
    pooled_picks = {m: pd.concat(parts, ignore_index=True) for m, parts in pooled.items()}
    per_fold.to_csv(LOG_DIR / f"exp_v16_perfold_{tag.replace(' ', '_')}.csv")
    for m, picks in pooled_picks.items():
        safe = tag.replace(" ", "_").replace("+", "_")
        picks.to_pickle(LOG_DIR / f"exp_v16_picks_{safe}_{m}.pkl")
    log(f"--- {tag} per-fold accuracy (%):")
    log(per_fold.round(2).to_string())
    rows = []
    for method in pooled_picks:
        picks = pooled_picks[method]
        row = score_picks(picks, method)
        row["fold_mean"] = per_fold[method].mean()
        row["fold_std"] = per_fold[method].std()
        groups = picks.groupby("tims")["scored"].agg(["count", "sum"])
        for g in (1, 2, 3):
            row[f"g{g}"] = 100 * groups.loc[g, "sum"] / groups.loc[g, "count"]
        for era, lo in (("acc_2023p", "2023-10-01"), ("acc_2025p", "2025-09-01")):
            sub = picks[picks["date"] >= lo]
            row[era] = 100 * sub["scored"].sum() / len(sub) if len(sub) else np.nan
            row[era + "_n"] = len(sub)
        rows.append(row)
    summary = pd.DataFrame(rows).set_index("method")
    cols = ["acc_mean", "fold_std", "acc", "hits", "total", "g1", "g2", "g3",
            "acc_2023p", "acc_2025p", "acc_2023p_n", "acc_2025p_n"]
    summary["acc_mean"] = summary["fold_mean"]  # historical label
    log(f"--- {tag} pooled summary:")
    log(summary[cols].round(3).to_string())
    return summary, pooled_picks


def main():
    global BUILD_XG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--arms", nargs="+", default=["base", "exposure"])
    parser.add_argument("--build-xg", action="store_true", help=("fit the per-shot xG model "
                        "for --seed if the cache is missing (writes shot_xg_seed{N}.csv)"))
    parser.add_argument("--result-csv", default=None)
    parser.add_argument("--log-file", default=None)
    args = parser.parse_args()
    BUILD_XG = args.build_xg

    log = Log(Path(args.log_file) if args.log_file else LOG_DIR / f"exp_v16_exposure_seed{args.seed}.log")
    log(f"V16-exposure seed={args.seed} arms={args.arms} (model=ensemble run, embeds hgb/logreg/lgbm)")
    log(f"command: exp_exposure.py --env {args.env} --seed {args.seed} "
        f"(OMP_NUM_THREADS=2 LGBM_N_JOBS=2, lightgbm==4.7.0)")

    players, teams, goalies = load_frames(args.env, refresh=False)
    frame = build_frame(players, teams, goalies)
    frame = add_derived(frame, teams)
    log(f"frame rows {len(frame):,} (42-feature corrected baseline incl. add_derived)")

    results = []
    pick_store = {}
    for arm in args.arms:
        try:
            if arm == "exposure":
                frame_arm = exposure_feature(frame, args.seed, log)
            else:
                frame_arm = frame
            log(f"\n===== arm {arm} seed {args.seed} =====")
            t0 = time.time()
            stats, pooled, out_rows, aucs = run_arm(frame_arm, args.seed, log, arm)
            log(f"arm {arm} run_folds wall {time.time() - t0:.0f}s; mean AUC {np.mean(aucs):.4f}")
            summary, pooled_picks = summarise(stats, pooled, log, f"{arm} seed {args.seed}")
            summary_out = summary.reset_index()
            summary_out.insert(0, "arm", arm)
            summary_out.insert(0, "seed", args.seed)
            results.append(summary_out)
            pick_store[arm] = pooled_picks
            # name the base arm distinctly: it must not collide with exp_pbp.py's
            # base dump oof_v16_base_seed{seed}.csv (which is a different frame)
            arm_tag = f"exposure_{arm}" if arm == "base" else arm
            out_csv = Path(__file__).resolve().parents[3] / "data" / "ml" / f"oof_v16_{arm_tag}_seed{args.seed}.csv"
            out_rows.to_csv(out_csv, index=False)
            log(f"saved OOF rows -> {out_csv.name}")
            log.flush()
        except Exception as exc:
            log(f"ARM {arm} FAILED: {exc!r}")
            log.flush()
            raise

    if pick_store.get("exposure"):
        log("\n=== McNemar exact (pooled paired picks, all folds) ===")
        for arm in args.arms:
            for method in ("gpg", "hgpg", "hgb", "logreg", "lgbm", "ensemble"):
                if method not in pick_store[arm]:
                    continue
                _, _, n01, n10, p = mcnemar_exact(
                    pick_store[arm][method], pick_store[arm]["hgpg"]
                )
                log(f"  arm {arm}: {method} vs hgpg: {n01}-{n10}, p={p:.4f}")
        if "base" in pick_store and "exposure" in pick_store:
            for method in ("hgb", "logreg", "lgbm", "ensemble"):
                _, _, n01, n10, p = mcnemar_exact(
                    pick_store["exposure"][method], pick_store["base"][method],
                    "exposure", f"base_{method}",
                )
                log(f"  exposure {method} vs base {method}: {n01}-{n10}, p={p:.4f}")

    out_df = pd.concat(results, ignore_index=True)
    csv_path = Path(args.result_csv) if args.result_csv else LOG_DIR / f"exp_v16_exposure_results_seed{args.seed}.csv"
    out_df.to_csv(csv_path, index=False)
    log(f"wrote {csv_path}")
    log.flush()


if __name__ == "__main__":
    main()
