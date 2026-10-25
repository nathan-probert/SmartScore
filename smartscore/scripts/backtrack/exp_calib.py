"""V16-calib: calibration/discrimination diagnostics + two-stage within-group model.

Part 1 (diagnostic mode) reads the pooled OOF rows saved by
``model.py --model ensemble --model-file`` for seeds 1 and 3 and produces:

  * picks re-derived with ``model.daily_picks`` on the saved ``ensemble`` rank
    column (must reproduce the logged 929 / 942 pooled hits),
  * accuracy by predicted-probability decile (pmax = max of the three
    component probabilities, pmean = their mean),
  * per-group realized-vs-predicted calibration bins over ALL challenge rows,
  * 'confident miss' vs 'lucky hit' pick profiles: which feature values differ,
  * hindsight upper bounds: hits if top-pmax-decile misses were flipped, and
    hits if confident misses had gone to the runner-up pick instead.

Part 2 (twostage mode) is the improvement attempt: a leak-free two-stage
wrapper. Stage 1 = the standard LGBM (config a) pointwise model, rerun per
fold; its out-of-fold score is fed to stage 2 as an extra feature together
with a 'date-difficulty' summary (per-(date,tims) pre-game pool aggregates +
per-fold stage-1 group statistics). Stage 2 = another LGBM (config a) refit on
train rows only, and the group top-1 pick is rerun on the stage-2 score.

Leak policy: stage-1 probabilities for TRAIN rows are inner out-of-fold fits
over train dates only (never the outer test block), so no train row's stage-1
score is informed by any label outside the train dates; test-row stage-1
scores come from the model fit on train dates. Pool aggregates are same-date
pre-game statistics (the same class as the ``rank_*`` features). No
same-game shift-derived columns are used anywhere.

USAGE
    cd SmartScore-backtrack
    OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \\
        smartscore/scripts/backtrack/exp_calib.py --env dev --mode diagnostic \\
        --seeds 1 3
    OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \\
        smartscore/scripts/backtrack/exp_calib.py --env dev --mode twostage --seed 1
"""

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import model  # noqa: E402

from scipy.stats import binomtest  # noqa: E402

from lightgbm import LGBMClassifier  # noqa: E402

CATS = ["lineup_position_group", "tims"]
# Per-(date,tims) pre-game pool aggregates: the "date-difficulty" summary.
# Same class of information as rank_*: only that slate's pre-game rates.
POOL_FEATURES = ["pool_size", "pool_hgpg_mean", "pool_hgpg_max", "pool_gpg_mean"]
# Stage-1-derived per-group statistics (per fold, from fold-local stage-1 fits).
STAGE1_GROUP_FEATURES = ["grp_stage1_top1", "grp_stage1_gap", "grp_stage1_mean"]
STAGE2_NUMERIC = model.NUMERIC_FEATURES + POOL_FEATURES + STAGE1_GROUP_FEATURES + ["stage1"]
STAGE2_FEATURES = STAGE2_NUMERIC + CATS

GPG_REFERENCE = 26.50  # % pooled k-fold
HGPG_REFERENCE = 28.05


# ---------------------------------------------------------------- helpers


def season_label(dates):
    """NHL season label like '20232024' from a date string (Aug-Jul boundary)."""
    dates = pd.Series(dates, dtype=str)
    year = dates.str[:4].astype(int)
    month = dates.str[5:7].astype(int)
    start = np.where(month >= 8, year, year - 1)
    start = pd.Series(start, index=dates.index)
    return start.astype(str) + (start + 1).astype(str)


def era_masks(picks):
    season = season_label(picks["date"])
    return season >= "20232024", season >= "20252026"


def mcnemar_exact(picks_a, picks_b):
    """Exact two-sided McNemar on paired (date,tims) picks; returns (b, c, p)."""
    merged = picks_a.merge(
        picks_b, on=["date", "tims"], suffixes=("_a", "_b"), validate="one_to_one"
    )
    only_a = int(((merged["scored_a"] == 1) & (merged["scored_b"] == 0)).sum())
    only_b = int(((merged["scored_a"] == 0) & (merged["scored_b"] == 1)).sum())
    n = only_a + only_b
    p = float(binomtest(min(only_a, only_b), n, 0.5).pvalue) if n else 1.0
    return only_a, only_b, p


def baseline_row(picks, tag):
    row = model.score_picks(picks, tag)
    row["fold_std"] = np.nan
    return row


def summarize_picks(picks, tag):
    """Pooled table row + per-group + era hit rates."""
    row = model.score_picks(picks, tag)
    groups = picks.groupby("tims")["scored"].agg(["count", "sum"])
    row["per_group"] = " ".join(
        f"g{int(t)}={100 * r['sum'] / r['count']:.1f}" for t, r in groups.iterrows()
    )
    late, recent = era_masks(picks)
    for name, mask in (("2023-24+", late), ("2025-26", recent)):
        sub = picks[mask]
        row[name] = f"{100 * sub['scored'].mean():.2f} ({int(sub['scored'].sum())}/{len(sub)})"
    return row


def print_summary_table(rows):
    frame = pd.DataFrame(rows).set_index("method")
    base_cols = ["total", "hits", "acc", "per_group", "2023-24+", "2025-26"]
    print(frame[base_cols].to_string())


def pool_group_features(frame):
    """Per-(date,tims) pre-game pool aggregates, including tims=0 slates."""
    grouped = frame.groupby(["date", "tims"], sort=False)
    frame = frame.copy()
    frame["pool_size"] = grouped["hgpg"].transform("size").astype(float)
    frame["pool_hgpg_mean"] = grouped["hgpg"].transform("mean")
    frame["pool_hgpg_max"] = grouped["hgpg"].transform("max")
    frame["pool_gpg_mean"] = grouped["gpg"].transform("mean")
    return frame


def group_stage_stats(part, col="stage1"):
    """Top-1/top-2 stage-1 stats within (date,tims); group is fold-local."""
    part = part.copy()
    grouped = part.groupby(["date", "tims"], sort=False)
    part["grp_stage1_top1"] = grouped[col].transform("max")
    part["grp_stage1_mean"] = grouped[col].transform("mean")
    ordered = part.sort_values(col, ascending=False)
    rank = ordered.groupby(["date", "tims"], sort=False).cumcount() + 1
    second = ordered.loc[rank == 2].set_index(["date", "tims"])[col]
    gap = part["grp_stage1_top1"] - second.reindex(
        pd.MultiIndex.from_arrays([part["date"], part["tims"]])
    ).to_numpy()
    part["grp_stage1_gap"] = pd.Series(gap, index=part.index)
    return part


def encode_categoricals(train, test, cats):
    """model.categorise semantics for an explicit feature list."""
    x_train = train.copy()
    x_test = test.copy()
    for col in cats:
        vocab = sorted(set(x_train[col].dropna().astype(str)))
        cdict = {value: value for value in vocab}
        x_train[col] = x_train[col].astype(str).map(cdict).astype("category")
        x_test[col] = x_test[col].astype(str).map(cdict).astype("category")
        x_test.loc[~x_test[col].isin(x_train[col].cat.categories), col] = np.nan
    return x_train, x_test


def fit_stage2(train_feats, test_feats):
    """Stage-2 LGBM (config a) on the extended feature list."""
    x_train, x_test = encode_categoricals(
        train_feats[STAGE2_FEATURES], test_feats[STAGE2_FEATURES], CATS
    )
    for col in CATS:
        x_test[col] = x_test[col].cat.set_categories(x_train[col].cat.categories)
    lgbm_model = LGBMClassifier(
        objective="binary",
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=0,
        n_jobs=int(os.environ.get("LGBM_N_JOBS", 4)),
        verbose=-1,
        **model.LGBM_CONFIGS["a"],
    )
    lgbm_model.fit(x_train, train_feats["scored"].astype(int))
    prob = lgbm_model.predict_proba(x_test)[:, 1]
    return pd.Series(prob, index=test_feats.index)


def oof_path(tag, seed):
    return model.CACHE_DIR / f"oof_{tag}_seed{seed}.csv"


def load_oof(tag, seed):
    path = oof_path(tag, seed)
    frame = pd.read_csv(path)
    # Season dtype hygiene: model.py casts to str; round-trips may parse ints.
    if "season" in frame:
        frame["season"] = frame["season"].astype("Int64").astype(str)
    return frame


def picks_from_scores(sim, col):
    """NaN-filtered per-(date,tims) idxmax, exactly like model.daily_picks."""
    return model.daily_picks(sim, col)


# ---------------------------------------------------------------- diagnostic


def run_diagnostic(args, frame):
    features = model.NUMERIC_FEATURES + model.CATEGORICAL_FEATURES

    pick_frames = []   # full OOF rows (for calibration over all challenge rows)
    pick_tables = []   # re-derived picks only
    for seed in args.seeds:
        oof = load_oof(args.oof_tag, seed)
        # The saved file already carries gpg/hgpg; don't duplicate them.
        feature_frame = frame[["date", "player_id"] + [c for c in features if c not in oof.columns]].copy()
        print(f"\n=== seed {seed}: {len(oof):,} OOF rows from {oof_path(args.oof_tag, seed).name} ===")
        merged = oof.merge(feature_frame, on=["date", "player_id"], how="left", validate="one_to_one")
        assert len(merged) == len(oof), "feature merge changed row count"
        cols = [c for c in merged.columns if c.endswith(("_x", "_y"))]
        assert not cols, f"merge produced suffix columns: {cols}"
        assert not merged[model.NUMERIC_FEATURES].isna().all().any(), "feature merge matched nothing"
        merged = merged.assign(
            pmax=merged[["hgb", "lgbm", "logreg"]].max(axis=1),
            pmean=merged[["hgb", "lgbm", "logreg"]].mean(axis=1),
        )
        pick_frames.append(merged)
        pick_tables.append(picks_from_scores(merged, "ensemble"))

        # Reproduction check: re-derive picks from the saved rank column.
        for method in ("ensemble", "lgbm", "logreg", "hgb"):
            picks = picks_from_scores(merged, method)
            row = model.score_picks(picks, method)
            g_txt = " ".join(
                f"g{int(t)}={100 * r['sum'] / r['count']:.1f}"
                for t, r in (picks.groupby("tims")["scored"].agg(["count", "sum"])).iterrows()
            )
            print(
                f"  {method:9s} pooled {row['hits']}/{row['total']} = {row['acc']:.3f}%"
                f" | per-day hits {row['hits_day']:.3f} | {g_txt}"
            )
        late, recent = era_masks(model.daily_picks(merged, "ensemble"))
        for name, mask in (("2023-24+", late), ("2025-26", recent)):
            sub = model.daily_picks(merged, "ensemble")[mask]
            print(f"  ensemble {name}: {100 * sub['scored'].mean():.2f}% ({int(sub['scored'].sum())}/{len(sub)})")

    all_picks = pd.concat(pick_tables, ignore_index=True)
    n_picks = len(all_picks)
    print(f"\nPooled over seeds {args.seeds}: {n_picks} picks, "
          f"realized {100 * all_picks['scored'].mean():.3f}%")

    # ---- decile tables (on picks) ----
    print("\n--- accuracy by predicted-probability decile (picks, pooled seeds) ---")
    for prob in ("pmax", "pmean"):
        dec = pd.qcut(all_picks[prob], 10, labels=False, duplicates="drop")
        table = all_picks.assign(decile=dec).groupby("decile")[[prob, "scored"]].agg(
            **{prob: (prob, "mean"), "n": ("scored", "size"), "hits": ("scored", "sum")}
        )
        table["hit_pct"] = 100 * table["hits"] / table["n"]
        print(f"\n[{prob} deciles]")
        print(table.round(4).to_string())

    # ---- calibration curves per group (ALL challenge OOF rows, pooled seeds) ----
    print("\n--- calibration by (date,tims) group, ALL challenge rows, pooled seeds ---")
    for tims in (1, 2, 3):
        rows = []
        for merged in pick_frames:
            rows.append(merged[merged["tims"] == tims])
        grp = pd.concat(rows, ignore_index=True)
        bins = pd.qcut(grp["pmean"], 5, labels=False, duplicates="drop")
        table = grp.assign(bin=bins).groupby("bin")[["pmean", "scored"]].agg(
            **{"pred_mean": ("pmean", "mean"), "n": ("scored", "size"), "hit_rate": ("scored", "mean")}
        )
        table["gap_pp"] = 100 * (table["pred_mean"] - table["hit_rate"])
        print(f"\n[g{tims}: predicted (pmean) vs realized, quintiles, n={len(grp):,}]")
        print(table.round(4).to_string())

    # ---- confident-miss vs lucky-hit profile ----
    print("\n--- confident-miss vs lucky-hit pick profile (pooled seeds) ---")
    q_hi = all_picks["pmax"].quantile(0.75)
    q_lo = all_picks["pmax"].quantile(0.25)
    cm = all_picks[(all_picks["pmax"] >= q_hi) & (all_picks["scored"] == 0)]
    ch = all_picks[(all_picks["pmax"] >= q_hi) & (all_picks["scored"] == 1)]
    lh = all_picks[(all_picks["pmax"] <= q_lo) & (all_picks["scored"] == 1)]
    print(f"pmax quartile thresholds: top>={q_hi:.4f}, bottom<={q_lo:.4f}")
    print(f"confident picks (top quartile): {len(cm) + len(ch):,} "
          f"(hits {len(ch):,} = {100 * len(ch) / max(len(cm) + len(ch), 1):.1f}%)")
    print(f"confident misses: {len(cm):,} | confident hits: {len(ch):,} | lucky hits (bottom-quartile hit): {len(lh):,}")

    num_features = model.NUMERIC_FEATURES
    cm_mean = cm[num_features].mean()
    lh_mean = lh[num_features].mean()
    pooled_sd = pd.concat([cm, lh])[num_features].std()
    diff = ((cm_mean - lh_mean) / pooled_sd.replace(0, np.nan)).rename("cohens_d")
    profile = pd.DataFrame({"cm_mean": cm_mean, "lh_mean": lh_mean, "cohens_d": diff}).sort_values(
        "cohens_d", key=np.abs, ascending=False
    )
    print("\nfeature profile, confident-miss (CM) minus lucky-hit (LH), standardized:")
    print(profile.round(4).to_string())

    # ---- hindsight upper bounds ----
    n_hits = int(all_picks["scored"].sum())
    dec10 = pd.qcut(all_picks["pmax"], 10, labels=False, duplicates="drop")
    top_label = int(dec10.max())
    top_decile = all_picks[dec10 == top_label]
    flips = int((top_decile["scored"] == 0).sum())
    print(f"\ntop pmax-decile threshold {top_decile['pmax'].min():.4f}; picks inside "
          f"{len(top_decile):,} (misses {flips:,})")
    print(f"upper bound: if ALL top-decile misses flipped to hits: "
          f"{n_hits + flips}/{n_picks} = {100 * (n_hits + flips) / n_picks:.3f}%")
    print(f"i.e. perfect within-top-decile discrimination could add at most "
          f"{100 * flips / n_picks:.2f}pp pooled")

    # runner-up hindsight among confident misses
    runner_hits = 0
    runner_total = 0
    for merged, picks_seed in zip(pick_frames, pick_tables):  # same seed's rows
        grouped = merged.groupby(["date", "tims"], sort=False)
        for _, pick in picks_seed[(picks_seed["pmax"] >= q_hi) & (picks_seed["scored"] == 0)].iterrows():
            members = grouped.get_group((pick["date"], pick["tims"]))
            others = members[members["pmax"] < pick["pmax"]].sort_values("pmax", ascending=False)
            if others.empty:
                continue
            runner_total += 1
            runner_hits += int(others.iloc[0]["scored"] == 1)
    print(f"runner-up hindsight among top-quartile misses: {runner_hits}/{runner_total} "
          f"would have hit ({100 * runner_hits / max(runner_total, 1):.1f}%)"
          " — the recoverable margin for a better within-group choice")

    # save pick-level detail for the notes
    keep = ["date", "player_id", "tims", "scored", "pmax", "pmean", "hgb", "lgbm", "logreg", "ensemble"]
    all_picks[keep].to_csv(model.CACHE_DIR / f"v16calib_picks_diag.csv", index=False)
    profile.to_csv(model.CACHE_DIR / "v16calib_feature_profile.csv")
    print(f"\nsaved pick-level and profile tables to {model.CACHE_DIR / 'v16calib_picks_diag.csv'}")


# ---------------------------------------------------------------- twostage


def run_twostage(args, frame):
    seed = args.seed
    print(f"=== two-stage model, seed {seed}, inner splits k={args.inner_splits} ===")
    reference = load_oof(args.oof_tag, seed)
    ref_picks = {
        tag: picks_from_scores(reference, tag)
        for tag in ("ensemble", "lgbm", "hgpg", "gpg")
    }
    frame = pool_group_features(frame)

    challenge = frame["tims"].isin([1, 2, 3]) & frame["scored"].notna()
    challenge_dates = frame.loc[challenge, "date"].unique()
    blocks = model.fold_blocks(challenge_dates, 5, seed)

    pooled = {"stage1": [], "stage2": [], "gpg": [], "hgpg": []}
    fold_stats = []
    pairing_max_diff = 0.0

    for number, block_dates in enumerate(blocks, start=1):
        test_dates = pd.Index(block_dates)
        test_all = frame[frame["date"].isin(test_dates)]
        test = test_all[challenge].copy()
        train = frame[~frame["date"].isin(test_dates) & frame["scored"].notna()]

        sim = test.copy()
        for tag, col in (("gpg", "gpg"), ("hgpg", "hgpg")):
            sim[tag] = test[col].astype(float).fillna(-np.inf)
            pooled[tag].append(picks_from_scores(sim, tag))

        # Stage 1: standard LGBM config a. Test rows scored by a model fit on
        # train dates; train rows scored by inner OOF over train dates only.
        prob_test = model.fit_lgbm(train, test, "a")
        saved_lgbm = reference.set_index(["date", "player_id"])["lgbm"]
        pairing_diff = float(
            np.nanmax(np.abs(prob_test.to_numpy() - saved_lgbm.reindex(test.set_index(["date", "player_id"]).index).to_numpy()))
        )
        pairing_max_diff = max(pairing_max_diff, pairing_diff)

        train_dates = train["date"].unique()
        stage1_train = pd.Series(np.nan, index=train.index)
        for inner_number, inner_dates in enumerate(
            model.fold_blocks(train_dates, args.inner_splits, seed * 100 + number), start=1
        ):
            inner_test = train[train["date"].isin(pd.Index(inner_dates))]
            inner_train = train[~train["date"].isin(pd.Index(inner_dates))]
            stage1_train.loc[inner_test.index] = model.fit_lgbm(inner_train, inner_test, "a").to_numpy()

        train_feats = train.copy()
        train_feats["stage1"] = stage1_train
        train_feats = group_stage_stats(train_feats)
        test_feats = test.copy()
        test_feats["stage1"] = prob_test
        test_feats = group_stage_stats(test_feats)

        prob_stage2 = fit_stage2(train_feats, test_feats)

        sim["stage1"] = prob_test.fillna(0).to_numpy()
        pooled["stage1"].append(picks_from_scores(sim, "stage1"))
        sim["stage2"] = prob_stage2.fillna(0).to_numpy()
        pooled["stage2"].append(picks_from_scores(sim, "stage2"))

        fold_acc = {tag: model.score_picks(p[-1], tag)["acc"] for tag, p in pooled.items()}
        fold_stats.append({"fold": number, **fold_acc})
        print(f"  fold {number}: {len(sim):,} sim rows, pairing diff {pairing_diff:.2e}, "
              + " ".join(f"{tag}={acc:.2f}%" for tag, acc in fold_acc.items()), flush=True)

    print(f"\nstage-1 pairing max |diff| vs saved lgbm column: {pairing_max_diff:.3e} "
          "(0 => fold blocks pair exactly with the model-file run)")

    per_fold = pd.DataFrame(fold_stats).set_index("fold")
    print("\nper-fold pick accuracy (%):")
    print(per_fold.round(2).to_string())
    print("\nper-fold mean +/- std:")
    for tag in pooled:
        print(f"  {tag:7s} {per_fold[tag].mean():.3f} +/- {per_fold[tag].std():.3f}")

    pooled_picks = {tag: pd.concat(parts, ignore_index=True) for tag, parts in pooled.items()}
    rows = [summarize_picks(picks, tag) for tag, picks in pooled_picks.items()]
    rows.append(summarize_picks(ref_picks["ensemble"], "ensemble_ref"))
    rows.append(summarize_picks(ref_picks["lgbm"], "lgbm_ref"))
    print("\npooled out-of-fold picks:")
    print_summary_table(rows)

    print("\nMcNemar exact, pooled paired picks:")

    def picks_of(tag):
        return pooled_picks[tag] if tag in pooled_picks else ref_picks[tag]

    tests = [
        ("stage2", "gpg"), ("stage2", "hgpg"),
        ("stage2", "ensemble_ref"), ("stage2", "lgbm_ref"),
        ("stage1", "gpg"), ("stage1", "hgpg"),
        ("ensemble_ref", "gpg"), ("ensemble_ref", "hgpg"),
    ]
    for a, b in tests:
        n_a, n_b, p = mcnemar_exact(picks_of(a), picks_of(b))
        print(f"  {a} vs {b}: {n_a}-{n_b}, p={p:.4f}")

    out = pooled_picks["stage2"][["date", "player_id", "tims", "scored"]]
    out.to_csv(model.CACHE_DIR / f"v16calib_stage2_picks_seed{seed}.csv", index=False)
    print(f"\nsaved stage-2 pooled picks to {model.CACHE_DIR / f'v16calib_stage2_picks_seed{seed}.csv'}")


def thresholded_picks(sim, prob_col, evidence_col, margin):
    """Within-group top-1 with a tie-band fallback to better binary evidence."""
    picks = []
    for _, members in sim.groupby(["date", "tims"], sort=False):
        finite = members[members[prob_col].notna()]
        if finite.empty:
            continue
        ordered = finite.sort_values(prob_col, ascending=False)
        top = ordered.iloc[0]
        if margin is not None and len(ordered) >= 2:
            second = ordered.iloc[1]
            ev_top = top[evidence_col]
            ev_sec = second[evidence_col]
            if (
                (top[prob_col] - second[prob_col]) < margin
                and pd.notna(ev_sec)
                and (pd.isna(ev_top) or ev_sec > ev_top)
            ):
                picks.append(second)
                continue
        picks.append(top)
    return pd.DataFrame(picks)


def run_threshold_oof(args):
    """Variant (a), frame-free: within-group top-1 competitor swap on the
    saved OOF probabilities. Evidence = hgpg (prior-game hit-rate support),
    which is already in the model-file dump, so no model.py frame build and
    no refits are needed. Margin calibrated per fold on TRAIN folds only:
    the train folds' own picks are recomputed under each candidate margin and
    the accuracy-maximising margin is applied to the test block."""
    seed = args.seed
    print(f"=== threshold-oof swap variant (evidence=hgpg), seed {seed} ===")
    oof = load_oof(args.oof_tag, seed)
    oof = oof.assign(pmean=oof[["hgb", "lgbm", "logreg"]].mean(axis=1))
    evidence_col = "hgpg"

    blocks = model.fold_blocks(oof["date"].unique(), 5, seed)
    margins = [0.01, 0.02, 0.05, 0.08, 0.10, 0.15, 0.25]

    ref_pick = picks_from_scores(oof, "ensemble")
    lgbm_pick = picks_from_scores(oof, "lgbm")
    hgpg_pick = picks_from_scores(oof, "hgpg")
    gpg_pick = picks_from_scores(oof, "gpg")
    rows = [summarize_picks(ref_pick, "ensemble_ref"), summarize_picks(lgbm_pick, "lgbm_ref"),
            summarize_picks(gpg_pick, "gpg"), summarize_picks(hgpg_pick, "hgpg")]

    pooled_fixed = {margin: [] for margin in margins}
    pooled_calibrated = []
    fold_rows = []
    for number, block_dates in enumerate(blocks, start=1):
        test_dates = pd.Index(block_dates)
        test = oof[oof["date"].isin(test_dates)]
        train = oof[~oof["date"].isin(test_dates)]
        train_acc = {}
        for margin in margins:
            picks = thresholded_picks(train, "pmean", evidence_col, margin)
            train_acc[margin] = 100 * picks["scored"].sum() / len(picks)
        best_margin = max(train_acc, key=lambda m: train_acc[m])
        row = {"fold": number, "best_margin": best_margin,
               "train_acc": train_acc[best_margin],
               "train_base": 100 * train["scored"].sum() / len(train)}
        for margin in margins:
            picks = thresholded_picks(test, "pmean", evidence_col, margin)
            pooled_fixed[margin].append(picks)
            row[f"te_{margin}"] = model.score_picks(picks, "x")["acc"]
        cal = thresholded_picks(test, "pmean", evidence_col, best_margin)
        pooled_calibrated.append(cal)
        row["te_calibrated"] = model.score_picks(cal, "x")["acc"]
        fold_rows.append(row)
        print(f"  fold {number}: train margin {best_margin} (train {train_acc[best_margin]:.2f}% "
              f"vs base {row['train_base']:.2f}%) -> test acc {row['te_calibrated']:.2f}%", flush=True)

    margin_table = pd.DataFrame(fold_rows).set_index("fold")
    print("\nper-fold test pick accuracy (%):")
    print(margin_table.round(2).to_string())
    print("\nper-fold mean +/- std:")
    cal_acc = margin_table["te_calibrated"]
    print(f"  swap-cal {cal_acc.mean():.3f} +/- {cal_acc.std():.3f}")

    rows.append(summarize_picks(pd.concat(pooled_calibrated, ignore_index=True), "swap_calibrated"))
    for margin in margins:
        rows.append(summarize_picks(pd.concat(pooled_fixed[margin], ignore_index=True), f"fixed_{margin}"))
    print("\npooled out-of-fold picks:")
    print_summary_table(rows)

    print("\nMcNemar exact, pooled paired picks (b c p):")
    cal_picks = pd.concat(pooled_calibrated, ignore_index=True)
    for tag, target in (("ensemble_ref", ref_pick), ("lgbm_ref", lgbm_pick), ("gpg", gpg_pick), ("hgpg", hgpg_pick)):
        n_a, n_b, p = mcnemar_exact(cal_picks, target)
        print(f"  swap calibrated vs {tag}: {n_a}-{n_b}, p={p:.4f}")
    for margin in margins:
        fixed = pd.concat(pooled_fixed[margin], ignore_index=True)
        n_e, n_r, _ = mcnemar_exact(fixed, ref_pick)
        n_h, n_g, _ = mcnemar_exact(fixed, hgpg_pick)
        print(f"  fixed {margin}: vs ensemble {n_e}-{n_r} | vs hgpg {n_h}-{n_g}")
    out = cal_picks[["date", "player_id", "tims", "scored"]]
    out.to_csv(model.CACHE_DIR / f"v16calib_swap_picks_seed{seed}.csv", index=False)
    print(f"\nsaved swap pooled picks to {model.CACHE_DIR / f'v16calib_swap_picks_seed{seed}.csv'}")


def run_threshold(args, frame):
    """Variant (c): thresholded choice on saved OOF probabilities."""
    for seed in args.seeds:
        _run_threshold_seed(args, frame, seed)


def _run_threshold_seed(args, frame, seed):
    print(f"=== thresholded-choice variant, seed {seed} ===")
    oof = load_oof(args.oof_tag, seed)
    # careful: the OOF file already has an hgpg column, drop the merge dupes
    frame_extra = frame[["date", "player_id", "career_goals_pg"]].copy()
    oof = oof.drop(columns=[c for c in ("career_goals_pg",) if c in oof.columns])
    oof = oof.merge(frame_extra, on=["date", "player_id"], how="left", validate="one_to_one")
    oof["pmean"] = oof[["hgb", "lgbm", "logreg"]].mean(axis=1)
    evidence_col = "career_goals_pg"

    blocks = model.fold_blocks(oof["date"].unique(), 5, seed)
    margins = [0.01, 0.02, 0.05, 0.08, 0.10, 0.15, 0.25]
    ref_pick = picks_from_scores(oof, "ensemble")
    hgpg_pick = picks_from_scores(oof, "hgpg")

    pooled_fixed = {margin: [] for margin in margins}
    pooled_calibrated = []
    fold_rows = []
    for number, block_dates in enumerate(blocks, start=1):
        test_dates = pd.Index(block_dates)
        test = oof[oof["date"].isin(test_dates)]
        train = oof[~oof["date"].isin(test_dates)]
        train_acc = {}
        for margin in margins:
            picks = thresholded_picks(train, "pmean", evidence_col, margin)
            train_acc[margin] = 100 * picks["scored"].sum() / len(picks)
        best_margin = max(train_acc, key=lambda m: train_acc[m])
        row = {"fold": number, "best_margin": best_margin}
        for margin in margins:
            picks = thresholded_picks(test, "pmean", evidence_col, margin)
            pooled_fixed[margin].append(picks)
            row[f"te_{margin}"] = model.score_picks(picks, "x")["acc"]
        cal = thresholded_picks(test, "pmean", evidence_col, best_margin)
        pooled_calibrated.append(cal)
        row["te_calibrated"] = model.score_picks(cal, "x")["acc"]
        row["te_margin0"] = model.score_picks(thresholded_picks(test, "pmean", evidence_col, None), "x")["acc"]
        fold_rows.append(row)
        print(f"  fold {number}: train-calibrated margin {best_margin} -> test acc {row['te_calibrated']:.2f}%", flush=True)

    margin_table = pd.DataFrame(fold_rows).set_index("fold")
    print("\nper-fold test accuracy (%):")
    print(margin_table.round(2).to_string())

    rows = [summarize_picks(ref_pick, "ensemble_ref")]
    for margin in margins:
        rows.append(summarize_picks(pd.concat(pooled_fixed[margin], ignore_index=True), f"fixed_{margin}"))
    rows.append(summarize_picks(pd.concat(pooled_calibrated, ignore_index=True), "calibrated"))
    print("\npooled out-of-fold picks:")
    print_summary_table(rows)

    print("\nMcNemar exact, pooled paired picks:")
    for margin in margins:
        picks = pd.concat(pooled_fixed[margin], ignore_index=True)
        n_e, n_r, p_e = mcnemar_exact(picks, ref_pick)
        n_h, n_g, p_h = mcnemar_exact(picks, hgpg_pick)
        print(f"  margin {margin}: vs ensemble {n_e}-{n_r}, p={p_e:.4f} | vs hgpg {n_h}-{n_g}, p={p_h:.4f}")
    cal_picks = pd.concat(pooled_calibrated, ignore_index=True)
    n_e, n_r, p_e = mcnemar_exact(cal_picks, ref_pick)
    n_h, n_g, p_h = mcnemar_exact(cal_picks, hgpg_pick)
    print(f"  calibrated: vs ensemble {n_e}-{n_r}, p={p_e:.4f} | vs hgpg {n_h}-{n_g}, p={p_h:.4f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--mode", choices=["diagnostic", "twostage", "threshold", "threshold-oof"], required=True)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 3], help="diagnostic mode seeds")
    parser.add_argument("--oof-tag", default="v16_calib_ens", help="OOF file tag under data/ml (oof_<tag>_seedN.csv)")
    parser.add_argument("--inner-splits", type=int, default=3)
    args = parser.parse_args()

    if args.mode in ("diagnostic", "threshold"):
        print("building model frame ...", flush=True)
        players, teams, goalies = model.load_frames(args.env)
        frame = model.build_frame(players, teams, goalies)
        frame = model.add_derived(frame, teams)
        print(f"frame: {len(frame):,} rows", flush=True)
    else:
        frame = None

    if args.mode == "diagnostic":
        run_diagnostic(args, frame)
    elif args.mode == "threshold":
        run_threshold(args, frame)
    elif args.mode == "threshold-oof":
        run_threshold_oof(args)
    else:
        run_twostage(args, frame)


if __name__ == "__main__":
    main()
