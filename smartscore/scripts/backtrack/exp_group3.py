"""V16 — attack the group-3 weakness (group-conditional weights + dedicated g3 model).

Baseline per-group accuracy (ensemble, pooled seeds): g1 39.9% / g2 27.2% /
g3 20.9%. Group 3 is the weakest tier of each date's slate and drags pooled
accuracy (ensemble mean 29.71%). Three declared variants, all the SAME k=5
challenge-date k-fold, --train-subset all, no model.py edits:

  base     - unchanged rank ensemble hgb+logreg+lgbm (reference; must
             reproduce model.py --model ensemble for the same --seed).
             NOTE: the data/ml cache now carries the V15 pre-2017 rows
             (617,310 rows). The quoted reference ensemble (seeds 1/3 =
             29.408/29.820) is the V6c/V14 2017+ default (427,240 training
             rows), so training rows are filtered to
             --train-min-date 2017-10-01 by default to keep every comparison
             on the reference's training universe (V15 verdict: keep 2017+
             training). Pass --train-min-date none to train on the full
             augmented cache instead.
  w2 / w4  - group-conditional sample weights: every training row weight 1
             except tims==3 rows, weighted W (2 / 4). All three ensemble
             components re-fit with the weights, ensemble scoring unchanged.
             Rationale: moving loss mass (~4.7% -> ~10% / ~24% of total) onto
             the g3-relevant distribution shapes changes the fitted function,
             so within-group orderings can change for ALL groups (a re-fit is
             not a monotone transform of the same model's scores).
  g3blend  - a dedicated LGBM classifier trained ONLY on tims==3 training rows
             (~24.6k rows/fold). By construction g1/g2 picks keep the base
             ensemble scores exactly; only the g3 pick can change. g3 score =
             0.5 * (percentile rank of base ensemble within (date,tims)) +
             0.5 * (percentile rank of the g3 model within (date,tims)).
  g3pure   - diagnostics only: g3 pick from the dedicated model alone
             (g1/g2 still base ensemble).

  znop     - per-group score normalisation (z-score within (date,tims)) is
             evaluated post-hoc on the saved base-OOF scores: within a group a
             z-score is an affine (monotone) transform, so the group top-1 is
             unchanged by construction. It can only matter when comparisons
             CROSS groups, which pick-time does not (picks are argmax within
             one (date,tims) group). The run therefore quantifies it (expect
             0 changed picks) instead of spending a training pass on it.

Usage:
  OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
      smartscore/scripts/backtrack/exp_group3.py --env dev --seed 1
"""

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# Same sys.path dance as model.py: config.py sits in smartscore/.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.append(os.path.dirname(__file__))

from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.impute import SimpleImputer  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from model import (  # noqa: E402
    DATASET_FEATURES,
    LGBM_CONFIGS,
    add_derived,
    build_frame,
    categorise,
    daily_picks,
    fit_hgb,
    fit_lgbm,
    fit_logreg,
    fold_blocks,
    load_frames,
    score_picks,
)

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "ml"


# --- weighted re-fits of the ensemble components (identical configs to
# fit_hgb / fit_lgbm / fit_logreg in model.py; only a sample_weight argument
# is added). model.py must not be edited, so they live here. ---


def fit_hgb_w(train, test, weights):
    x_train, x_test = categorise(train, test)
    model = HistGradientBoostingClassifier(
        learning_rate=0.03,
        max_iter=1200,
        max_leaf_nodes=31,
        l2_regularization=1.0,
        categorical_features="from_dtype",
        early_stopping=False,
        random_state=0,
    )
    model.fit(x_train, train["scored"].astype(int), sample_weight=weights)
    return pd.Series(model.predict_proba(x_test)[:, 1], index=test.index)


def fit_lgbm_w(train, test, weights, config="a"):
    from lightgbm import LGBMClassifier

    x_train, x_test = categorise(train, test)
    for col in ["lineup_position_group", "tims"]:
        x_test[col] = x_test[col].cat.set_categories(x_train[col].cat.categories)
    model = LGBMClassifier(
        objective="binary",
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=0,
        n_jobs=int(os.environ.get("LGBM_N_JOBS", 4)),
        verbose=-1,
        **LGBM_CONFIGS[config],
    )
    model.fit(x_train, train["scored"].astype(int), sample_weight=weights)
    return pd.Series(model.predict_proba(x_test)[:, 1], index=test.index)


def fit_logreg_weighted(train, test, weights):
    from model import NUMERIC_FEATURES

    model = make_pipeline(
        SimpleImputer(strategy="median"),
        StandardScaler(),
        LogisticRegression(max_iter=2000),
    )
    step = model.steps[-1][0]
    model.fit(train[NUMERIC_FEATURES], train["scored"].astype(int), **{f"{step}__sample_weight": weights})
    return pd.Series(model.predict_proba(test[NUMERIC_FEATURES])[:, 1], index=test.index)


def fit_g3_model(train_g3, test):
    """Dedicated group-3 classifier: LGBM config a on tims==3 rows only."""
    from lightgbm import LGBMClassifier

    x_train, x_test = categorise(train_g3, test)
    for col in ["lineup_position_group", "tims"]:
        x_test[col] = x_test[col].cat.set_categories(x_train[col].cat.categories)
    model = LGBMClassifier(
        objective="binary",
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=0,
        n_jobs=int(os.environ.get("LGBM_N_JOBS", 4)),
        verbose=-1,
        **LGBM_CONFIGS["a"],
    )
    model.fit(x_train, train_g3["scored"].astype(int))
    return pd.Series(model.predict_proba(x_test)[:, 1], index=test.index)


# --- evaluation helpers ---


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
    return a, b, p


def per_group(picks):
    groups = picks.groupby("tims")["scored"].agg(["count", "sum"])
    return " ".join(
        f"g{int(t)}={100 * row['sum'] / row['count']:.1f}" for t, row in groups.iterrows()
    )


def subset(picks, start):
    sub = picks[picks["date"] >= start]
    return int((sub["scored"] == 1).sum()), len(sub)


def summarize(arm, picks, fold_accs):
    hits = int((picks["scored"] == 1).sum())
    total = len(picks)
    h2324, n2324 = subset(picks, "2023-08-01")
    h2526, n2526 = subset(picks, "2025-08-01")
    return {
        "arm": arm,
        "hits": hits,
        "total": total,
        "acc": 100 * hits / total if total else np.nan,
        "fold_mean": np.mean(fold_accs),
        "fold_std": np.std(fold_accs),
        "groups": per_group(picks),
        "hit2324": f"{h2324}/{n2324}={100 * h2324 / n2324:.2f}%",
        "hit2526": f"{h2526}/{n2526}={100 * h2526 / n2526:.2f}%",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--splits", type=int, default=5)
    parser.add_argument("--w2", type=float, default=2.0, help="sample weight for tims==3 rows, arm w2")
    parser.add_argument("--w4", type=float, default=4.0, help="sample weight for tims==3 rows, arm w4")
    parser.add_argument("--oof-file", type=Path, default=None)
    parser.add_argument(
        "--train-min-date",
        default="2017-10-01",
        help="Training rows must be on/after this date. Default keeps the reference "
        "ensemble's 2017+ training universe (the cache now also holds the V15 "
        "pre-2017 rows, which the quoted references never trained on). 'none' trains "
        "on every cached row.",
    )
    parser.add_argument(
        "--rebuild-frame",
        action="store_true",
        help="rebuild the pickled frame cache instead of loading it",
    )
    parser.add_argument("--smoke", action="store_true", help="use only the first ~120 challenge dates")
    args = parser.parse_args()

    t0 = time.time()
    frame_path = CACHE_DIR / "v16_group3_frame.pkl"
    if frame_path.exists() and not args.rebuild_frame:
        frame = pd.read_pickle(frame_path)
        print(f"loaded cached frame: {len(frame):,} rows <- {frame_path.name}")
    else:
        players, teams, goalies = load_frames(args.env)
        frame = build_frame(players, teams, goalies)
        frame = add_derived(frame, teams)
        frame.to_pickle(frame_path)
        print(f"frame built: {len(frame):,} rows in {time.time() - t0:.0f}s -> {frame_path.name}")
    print(f"frame built: {len(frame):,} rows", flush=True)

    challenge = frame["tims"].isin([1, 2, 3]) & frame["scored"].notna()
    challenge_dates = frame.loc[challenge, "date"].unique()
    if args.smoke:
        challenge_dates = sorted(challenge_dates)[:120]
    blocks = fold_blocks(challenge_dates, args.splits, args.seed)

    arms = ["gpg", "hgpg", "base", f"w{args.w2:g}", f"w{args.w4:g}", "g3blend", "g3pure"]
    pooled = {arm: [] for arm in arms}
    fold_accs = {arm: [] for arm in arms}
    oof_rows = []

    for number, block_dates in enumerate(blocks, start=1):
        tf = time.time()
        test_dates = pd.Index(block_dates)
        test = frame[challenge & frame["date"].isin(test_dates)].copy()
        train_all = frame[~frame["date"].isin(test_dates) & frame["scored"].notna()]
        if args.train_min_date != "none":
            train_all = train_all[train_all["date"] >= args.train_min_date]
        n_train = len(train_all)

        sim = test.copy()
        for tag, col in (("gpg", "gpg"), ("hgpg", "hgpg")):
            sim[tag] = test[col].astype(float).fillna(-np.inf)
            picks = daily_picks(sim, tag)
            pooled[tag].append(picks)
            fold_accs[tag].append(score_picks(picks, tag)["acc"])

        # --- default ensemble (base arm) ---
        sim["hgb"] = fit_hgb(train_all, test).fillna(0).values
        sim["lgbm"] = fit_lgbm(train_all, test).fillna(0).values
        sim["logreg"] = fit_logreg(train_all, test).fillna(0).values
        ranks = sim.groupby(["date", "tims"])[["hgb", "lgbm", "logreg"]].rank(pct=True)
        sim["base"] = ranks.mean(axis=1)

        # --- group-conditional weighting arms ---
        for wtag, w in ((f"w{args.w2:g}", args.w2), (f"w{args.w4:g}", args.w4)):
            weights = np.where(train_all["tims"].to_numpy() == 3, w, 1.0)
            sim["w_hgb"] = fit_hgb_w(train_all, test, weights).fillna(0).values
            sim["w_lgbm"] = fit_lgbm_w(train_all, test, weights).fillna(0).values
            sim["w_logreg"] = fit_logreg_weighted(train_all, test, weights).fillna(0).values
            wranks = sim.groupby(["date", "tims"])[["w_hgb", "w_lgbm", "w_logreg"]].rank(pct=True)
            sim[wtag] = wranks.mean(axis=1)

        # --- dedicated group-3 model (trained only on tims==3 rows) ---
        train_g3 = train_all[train_all["tims"] == 3]
        sim["g3m"] = fit_g3_model(train_g3, test).fillna(0).values
        blend_ranks = sim.groupby(["date", "tims"])[["base", "g3m"]].rank(pct=True)
        sim["g3blend"] = np.where(
            sim["tims"].to_numpy() == 3,
            (0.5 * blend_ranks["base"].to_numpy() + 0.5 * blend_ranks["g3m"].to_numpy()),
            sim["base"].to_numpy(),
        )
        sim["g3pure"] = np.where(sim["tims"].to_numpy() == 3, sim["g3m"].to_numpy(), sim["base"].to_numpy())

        for arm in ("base", f"w{args.w2:g}", f"w{args.w4:g}", "g3blend", "g3pure"):
            picks = daily_picks(sim, arm)
            pooled[arm].append(picks)
            fold_accs[arm].append(score_picks(picks, arm)["acc"])

        keep = ["date", "player_id", "tims", "scored", "gpg", "hgpg", "base", "g3m", "g3blend", "g3pure"]
        keep += [f"w{args.w2:g}", f"w{args.w4:g}"]
        oof_rows.append(sim[keep].assign(fold=number))
        auc = roc_auc_score(test["scored"].astype(int), sim["base"])
        print(
            f"  fold {number}: train {n_train:,} rows, {len(sim):,} sim rows, base AUC {auc:.4f}, fold accs "
            + ", ".join(f"{a}={fold_accs[a][-1]:.1f}" for a in arms)
            + f" ({time.time() - tf:.0f}s)",
            flush=True,
        )

    pooled_picks = {arm: pd.concat(parts, ignore_index=True) for arm, parts in pooled.items()}

    print("\n=== pooled out-of-fold picks ===")
    rows = [summarize(arm, pooled_picks[arm], fold_accs[arm]) for arm in arms]
    for row in rows:
        print(
            f"{row['arm']:8s} {row['hits']}/{row['total']} = {row['acc']:.3f}% "
            f"(folds {row['fold_mean']:.3f} +- {row['fold_std']:.3f}) | {row['groups']} | "
            f"2023-24+ {row['hit2324']} | 2025-26 {row['hit2526']}"
        )

    print("\n=== exact McNemar (variant vs reference, pooled paired picks) ===")
    for arm in arms:
        for ref in ("gpg", "hgpg", "base"):
            if arm == ref:
                continue
            a, b, p = paired_counts(pooled_picks[arm], pooled_picks[ref])
            m2 = pooled_picks[arm][["date", "tims", "player_id"]].merge(
                pooled_picks[ref][["date", "tims", "player_id"]], on=["date", "tims"], suffixes=("_v", "_b")
            )
            changed = int((m2["player_id_v"] != m2["player_id_b"]).sum())
            print(
                f"{arm:8s} vs {ref:5s}: {a}-{b}, p={p:.4f} (n={len(m2)}, changed picks={changed})"
            )

    # --- save the OOF rows FIRST: the diagnostic below must never be able to
    # crash a finished run and lose hours of compute ---
    oof = pd.concat(oof_rows, ignore_index=True)
    if args.oof_file:
        args.oof_file.parent.mkdir(parents=True, exist_ok=True)
        oof.to_csv(args.oof_file, index=False)
        print(f"saved OOF rows -> {args.oof_file}")

    # --- per-group z-score no-op check on the base ensemble scores ---
    print("\n=== per-group z-score normalisation (expect 0 changed picks) ===")
    # ddof=0: a single-row (date, tims) group has sample std NaN, which `or 1.0`
    # does not catch (NaN is truthy) - that used to drop the group from the
    # z-side pick list and crash the positional compare below.
    z = oof.groupby(["date", "tims"])["base"].transform(lambda s: (s - s.mean()) / (s.std(ddof=0) or 1.0))
    oof["_z"] = z
    pick_raw = oof.loc[oof["base"].notna()].groupby(["date", "tims"])["base"].idxmax()
    pick_z = oof.loc[oof["_z"].notna()].groupby(["date", "tims"])["_z"].idxmax()
    same = (oof.loc[pick_raw, "player_id"].to_numpy() == oof.loc[pick_z, "player_id"].to_numpy()).all()
    n_groups = len(pick_raw)
    print(f"groups compared: {n_groups}; identical top-1 picks: {same}")
    print(f"total {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
