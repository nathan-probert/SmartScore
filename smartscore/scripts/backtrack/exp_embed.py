"""V16-embed: leak-free player latent factors as extra features (pure numpy).

Hypothesis (untried): a small low-rank factorization of player-season outcome
aggregates captures finishing skill / aging beyond the aggregate rate features
(gpg / career rates / V4 shot block) that empirical-Bayes shrinkage (V8) and
the V12 identity features already failed to add.

Design
------
* Pure-numpy weighted ALS - NO torch. Per fold, build a
  players x (season x channel) outcome matrix from the fold's TRAINING dates
  only, using raw ``player_games`` lines (goals / shots / toi):

      gpg = goals / gp      (weight = gp)
      spg = shots / gp      (weight = gp)
      fin = goals / shots   (weight = shots, observed where shots > 0)
      tpg = toi_min / gp    (weight = gp where toi present)

  Weighted column z-score, then ridge-regularised weighted ALS
  (Mz ~ U V', rank 2-5) via alternating batched small linear solves.
* Factor columns are fit ONLY on each fold's training dates and then applied
  to the held-out block (no global prefit across all dates). A test player or
  season unseen in the fold's training vocabulary emits NaN, which is filled
  with the fold's train-row mean (0 after standardization).
* Emitted feature columns (dim + 4):
      e1..e{d}              player factor, standardized on train rows
      e_rec_gpg/spg/fin/tpg U[player] . V[(season, channel)] - the factor
                            model's cross-season prediction of this player's
                            rate in this season (z-units), i.e. a low-rank
                            denoised / aged version of the season cell
* Comparison: the same-fold LGBM config a WITHOUT the factor columns runs in
  the same process (exact paired baseline), plus gpg/hgpg pick baselines and
  the documented reference OOF files for McNemar.

Usage
-----
    cd SmartScore-backtrack
    OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \\
        smartscore/scripts/backtrack/exp_embed.py --env dev --seed 1 \\
        --save-oof data/ml/oof_v16_embed_lgbm_seed1.csv
"""

import argparse
import gc
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import model as M  # noqa: E402

CHANNELS = ("gpg", "spg", "fin", "tpg")


def binom_two_sided(k, n):
    """Exact McNemar p-value: two-sided binomial on discordant pairs."""
    from scipy.stats import binomtest

    if n == 0:
        return 1.0
    return float(binomtest(k, n, 0.5).pvalue)


def mcnemar(picks_a, picks_b):
    """Paired exact McNemar between two pick tables keyed (date, tims).

    Returns (a_right_b_wrong, a_wrong_b_right, p) with a = first table.
    """
    a = picks_a.set_index(["date", "tims"])["scored"]
    b = picks_b.set_index(["date", "tims"])["scored"]
    joined = pd.concat([a.rename("sa"), b.rename("sb")], axis=1, join="inner")
    sa = joined["sa"] == 1
    sb = joined["sb"] == 1
    n10 = int((sa & ~sb).sum())  # a hits, b misses
    n01 = int((~sa & sb).sum())  # a misses, b hits
    return n10, n01, binom_two_sided(min(n10, n01), n10 + n01)


def picks_of(sim, col):
    return M.daily_picks(sim, col)


def roc_auc(y, score):
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(np.asarray(y), np.asarray(score)))


def goal_lines():
    """Raw per player-game goals / shots / toi lines from the raw store."""
    con = sqlite3.connect(f"file:{M.RAW_DB}?mode=ro", uri=True)
    df = pd.read_sql_query(
        "SELECT game_date AS date, season, player_id, goals, shots, toi FROM player_games",
        con,
    )
    con.close()
    df["date"] = df["date"].astype(str)
    df["season"] = df["season"].astype("Int64").astype(str)
    df["goals"] = pd.to_numeric(df["goals"], errors="coerce")
    df["shots"] = pd.to_numeric(df["shots"], errors="coerce")
    df["toi_min"] = df["toi"].apply(
        lambda value: (
            int(str(value).split(":")[0]) + int(str(value).split(":")[1]) / 60
            if isinstance(value, str) and ":" in str(value)
            else np.nan
        )
    )
    return df[["date", "season", "player_id", "goals", "shots", "toi_min"]]


def fit_factors(train_lines, dim, lam, iters, fold_no):
    """Weighted-ALS player factors fitted only on the fold's training lines.

    Matrix: players x (season x channel); entries are outcome rates, weights
    the associated sample size (games, or shots for the finishing channel).
    Missing cells (player not in that season, or no shots / no toi) are
    unobserved and skipped by the weighted solves. Returns the player factors
    U, column loadings V and the lookups needed to emit features.
    """
    grouped = train_lines.groupby(["player_id", "season"], sort=False)
    agg = grouped.agg(goals=("goals", "sum"), shots=("shots", "sum"), gp=("goals", "size"))
    # min_count=1: a player-season with no usable toi lines stays unobserved.
    agg["toi"] = grouped["toi_min"].sum(min_count=1)
    agg = agg.reset_index()
    players = np.sort(agg["player_id"].unique())
    seasons = np.sort(agg["season"].unique())
    p_pos = {p: i for i, p in enumerate(players)}
    s_pos = {s: i for i, s in enumerate(seasons)}
    n, n_s, n_ch = len(players), len(seasons), len(CHANNELS)
    vals = np.zeros((n, n_s * n_ch))
    wts = np.zeros((n, n_s * n_ch))

    rows_p = agg["player_id"].map(p_pos).to_numpy(dtype=np.int64)
    gp = agg["gp"].to_numpy(float)
    goals = agg["goals"].fillna(0.0).to_numpy(float)
    shots = agg["shots"].fillna(0.0).to_numpy(float)
    toi = agg["toi"].to_numpy(float)
    for j, ch in enumerate(CHANNELS):
        col = agg["season"].map(s_pos).to_numpy() * n_ch + j
        if ch == "gpg":
            v, w = goals / gp, gp
        elif ch == "spg":
            v, w = shots / gp, gp
        elif ch == "fin":
            ok = shots > 0
            w = np.where(ok, shots, 0.0)
            v = np.where(ok, goals / np.where(ok, shots, 1.0), 0.0)
        else:  # tpg
            ok = np.isfinite(toi)
            w = np.where(ok, gp, 0.0)
            v = np.where(ok, toi / np.where(ok, gp, 1.0), 0.0)
        vals[rows_p, col] = v
        wts[rows_p, col] = w

    # Weighted per-column z-score; drop degenerate columns (<40 observations
    # or ~zero weighted variance carry no cross-player structure).
    sw = wts.sum(axis=0)
    mean = (wts * vals).sum(axis=0) / np.where(sw > 0, sw, 1.0)
    var = (wts * (vals - mean) ** 2).sum(axis=0) / np.where(sw > 0, sw, 1.0)
    sd = np.sqrt(np.maximum(var, 0.0))
    keep = (sw >= 40.0) & (sd > 1e-9)
    vals = vals[:, keep]
    wts = wts[:, keep]
    mean, sd = mean[keep], sd[keep]
    kept_pos = np.flatnonzero(keep)
    col_keys = [(str(seasons[pos // n_ch]), CHANNELS[pos % n_ch]) for pos in kept_pos]
    mz = (vals - mean) / sd

    # SVD init on the mean-filled matrix (unobserved cells carry the column
    # mean 0 after z-scoring), then weighted-ALS refinement.
    U0, s0, V0t = np.linalg.svd(mz, full_matrices=False)
    U = U0[:, :dim] * np.sqrt(s0[:dim])
    V = (V0t[:dim].T * np.sqrt(s0[:dim]))
    eye = lam * np.eye(dim)
    wmz = wts * mz
    rmse = np.inf
    for _ in range(iters):
        # V given U: A_c = U' diag(w_c) U + lam I, b_c = U' (w_c m_c)
        A = np.einsum("pc,pd,pe->cde", wts, U, U) + eye
        b = wmz.T @ U
        V = np.linalg.solve(A, b[..., None])[..., 0]
        # U given V: A_p = V' diag(w_p) V + lam I, b_p = V' (w_p m_p)
        A = np.einsum("pc,cd,ce->pde", wts, V, V) + eye
        b = wmz @ V
        U = np.linalg.solve(A, b[..., None])[..., 0]
        resid = (mz - U @ V.T) ** 2
        rmse = float(np.sqrt((wts * resid).sum() / max(wts.sum(), 1.0)))

    col_pos = {key: i for i, key in enumerate(col_keys)}
    return {
        "U": U,
        "V": V,
        "dim": dim,
        "p_pos": p_pos,
        "col_pos": col_pos,
        "n_players": n,
        "n_seasons": n_s,
        "n_cols": len(col_keys),
        "obs_share": float((wts > 0).mean()),
        "rmse": rmse,
    }


def factor_cols(dim):
    return [f"e{i + 1}" for i in range(dim)] + [f"e_rec_{ch}" for ch in CHANNELS]


def emit_factors(emb, rows):
    """Frozen feature emission for any row set (train or test).

    A player unseen in the fold's training vocabulary gets NaN factors
    (filled with the train mean at standardization time); ditto a season
    whose column did not survive or was not observed.
    """
    n = len(rows)
    d = emb["dim"]
    cols = factor_cols(d)
    out = pd.DataFrame(np.nan, index=rows.index, columns=cols, dtype=float)
    pid = rows["player_id"].to_numpy()
    p_idx = np.array([emb["p_pos"].get(p, -1) for p in pid], dtype=np.int64)
    has_p = p_idx >= 0
    if has_p.any():
        out.loc[has_p, [f"e{i + 1}" for i in range(d)]] = emb["U"][p_idx[has_p]]
    season = rows["season"].to_numpy().astype(str)
    for ch in CHANNELS:
        c_idx = np.array([emb["col_pos"].get((s, ch), -1) for s in season], dtype=np.int64)
        ok = has_p & (c_idx >= 0)
        if ok.any():
            dots = (emb["U"][p_idx[ok]] * emb["V"][c_idx[ok]]).sum(axis=1)
            out.loc[ok, f"e_rec_{ch}"] = dots
    return out


def standardize(train_emb, test_emb):
    """Train-row stats only; frozen, then applied to test. NaN -> 0 (= mean)."""
    for col in train_emb.columns:
        v = train_emb[col].to_numpy(dtype=float)
        mu = float(np.nanmean(v)) if np.isfinite(v).any() else 0.0
        sd = float(np.nanstd(v)) if np.isfinite(v).any() else 1.0
        if not np.isfinite(sd) or sd <= 0:
            mu, sd = 0.0, 1.0
        train_emb[col] = (train_emb[col] - mu) / sd
        test_emb[col] = (test_emb[col] - mu) / sd
        train_emb[col] = train_emb[col].fillna(0.0)
        test_emb[col] = test_emb[col].fillna(0.0)


def categorise_ext(train, test, extra_cols):
    """model.categorise plus appended numeric columns on both sides."""
    x_train, x_test = M.categorise(train, test)
    for col in M.CATEGORICAL_FEATURES:
        x_test[col] = x_test[col].cat.set_categories(x_train[col].cat.categories)
    if extra_cols:
        x_train = pd.concat([x_train, train[extra_cols]], axis=1)
        x_test = pd.concat([x_test, test[extra_cols]], axis=1)
    return x_train, x_test


def fit_lgbm_ext(train, test, extra_cols):
    """model.fit_lgbm (config a) plus appended numeric columns."""
    from lightgbm import LGBMClassifier

    x_train, x_test = categorise_ext(train, test, extra_cols)
    model = LGBMClassifier(
        objective="binary",
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=0,
        n_jobs=int(os.environ.get("LGBM_N_JOBS", 4)),
        verbose=-1,
        **M.LGBM_CONFIGS["a"],
    )
    model.fit(x_train, train["scored"].astype(int))
    return pd.Series(model.predict_proba(x_test)[:, 1], index=test.index)


def fit_hgb_ext(train, test, extra_cols):
    """model.fit_hgb (identical hyper-parameters) plus appended columns."""
    from sklearn.ensemble import HistGradientBoostingClassifier

    x_train, x_test = categorise_ext(train, test, extra_cols)
    model = HistGradientBoostingClassifier(
        learning_rate=0.03,
        max_iter=1200,
        max_leaf_nodes=31,
        l2_regularization=1.0,
        categorical_features="from_dtype",
        early_stopping=False,
        random_state=0,
    )
    model.fit(x_train, train["scored"].astype(int))
    return pd.Series(model.predict_proba(x_test)[:, 1], index=test.index)


def fit_logreg_ext(train, test, extra_cols):
    """model.fit_logreg with factor columns appended to the numeric block."""
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    cols = list(M.NUMERIC_FEATURES) + list(extra_cols)
    pipe = make_pipeline(
        SimpleImputer(strategy="median"),
        StandardScaler(),
        LogisticRegression(max_iter=2000),
    )
    pipe.fit(train[cols], train["scored"].astype(int))
    return pd.Series(pipe.predict_proba(test[cols])[:, 1], index=test.index)


def season_start_year(date):
    year = int(str(date)[:4])
    return year if int(str(date)[5:7]) >= 8 else year - 1


def load_reference(base_dir, args, tag, filename):
    path = base_dir / filename
    if not path.exists():
        print(f"  reference OOF missing, skipping: {path}", flush=True)
        return None
    ref = pd.read_csv(path)
    return picks_of(ref, tag)


def run(args):
    t0 = time.time()
    players, teams, goalies = M.load_frames(args.env)
    frame = M.build_frame(players, teams, goalies)
    # Free the raw CSV caches before the heavy add_derived pass - the box is
    # memory-tight whenever other backtrack workers are active.
    del players, goalies
    gc.collect()
    frame = M.add_derived(frame, teams)
    del teams
    gc.collect()
    print(f"frame built: {len(frame):,} rows in {time.time() - t0:.0f}s", flush=True)

    lines = goal_lines()
    gc.collect()
    print(f"player_games lines: {len(lines):,} rows "
          f"({lines['season'].nunique()} seasons) in {time.time() - t0:.0f}s", flush=True)

    challenge = frame["tims"].isin([1, 2, 3]) & frame["scored"].notna()
    challenge_dates = frame.loc[challenge, "date"].unique()
    blocks = M.fold_blocks(challenge_dates, args.splits, args.seed)
    ecols = factor_cols(args.embed_dim)
    use_ensemble = "ensemble" in args.models

    base_dir = Path(__file__).resolve().parents[3] / "data" / "ml"
    ref_lgbm = load_reference(base_dir, args, "lgbm", f"oof_v7_base_lgbm_seed{args.seed}.csv")
    ref_ens = None
    if use_ensemble:
        ref_ens = load_reference(base_dir, args, "ensemble", f"oof_v6c_ensemble_seed{args.seed}.csv")
        if ref_ens is not None:
            hits = int((ref_ens["scored"] == 1).sum())
            print(f"reference ensemble picks ({args.seed}): {hits}/{len(ref_ens)} "
                  f"= {100 * hits / len(ref_ens):.3f}%", flush=True)

    fold_acc = {}
    picks_by_model = {}
    aucs = {}
    embed_meta = []
    sims = []

    for fold_no, block in enumerate(blocks, start=1):
        if args.max_folds and fold_no > args.max_folds:
            break
        test_dates = pd.Index(block)
        test_all = frame[frame["date"].isin(test_dates)]
        test = test_all[test_all["tims"].isin([1, 2, 3]) & test_all["scored"].notna()].copy()
        train = frame[~frame["date"].isin(test_dates) & frame["scored"].notna()]  # --train-subset all
        overlap = set(train["date"].unique()) & set(test["date"].unique())
        assert not overlap, f"leak: fold train/test dates overlap: {overlap}"

        sim = test.copy()
        for tag, col in (("gpg", "gpg"), ("hgpg", "hgpg")):
            sim[tag] = test[col].astype(float).fillna(-np.inf)
            picks_by_model.setdefault(tag, []).append(picks_of(sim, tag))

        # Factors from THIS fold's training dates only.
        t_als = time.time()
        train_dates = set(train["date"].unique())
        emb = fit_factors(
            lines[lines["date"].isin(train_dates)],
            args.embed_dim, args.als_reg, args.als_iters, fold_no,
        )
        train_emb = emit_factors(emb, train)
        test_emb = emit_factors(emb, test)
        standardize(train_emb, test_emb)
        train = pd.concat([train, train_emb], axis=1)
        test = pd.concat([test, test_emb], axis=1)
        sim = pd.concat([sim, test_emb], axis=1)
        cov = float(test_emb["e1"].notna().mean())
        # How much do the reconstruction columns overlap plain gpg?
        gpg_train = train["gpg"].to_numpy(dtype=float)
        corr_gpg = float(np.corrcoef(train_emb["e_rec_gpg"], np.nan_to_num(gpg_train))[0, 1])
        embed_meta.append({
            "fold": fold_no, "matrix": f"{emb['n_players']}x{emb['n_cols']}",
            "obs_share": emb["obs_share"], "rmse": emb["rmse"],
            "test_cov": cov, "corr_rec_gpg": corr_gpg,
            "als_s": round(time.time() - t_als, 1),
        })
        print(f"  fold {fold_no} factors: {emb['n_players']} players x {emb['n_cols']} cols, "
              f"obs {emb['obs_share']:.2f}, rmse {emb['rmse']:.4f}, test cov {cov:.3f}, "
              f"corr(e_rec_gpg, gpg) {corr_gpg:.3f}, {time.time() - t_als:.0f}s", flush=True)

        sim["lgbm"] = fit_lgbm_ext(train, test, []).fillna(0).values
        picks_by_model.setdefault("lgbm", []).append(picks_of(sim, "lgbm"))
        aucs.setdefault("lgbm", []).append(roc_auc(test["scored"].astype(int), sim["lgbm"]))
        sim["lgbm_e"] = fit_lgbm_ext(train, test, ecols).fillna(0).values
        picks_by_model.setdefault("lgbm_e", []).append(picks_of(sim, "lgbm_e"))
        aucs.setdefault("lgbm_e", []).append(roc_auc(test["scored"].astype(int), sim["lgbm_e"]))
        if use_ensemble:
            sim["hgb_e"] = fit_hgb_ext(train, test, ecols).fillna(0).values
            sim["logreg_e"] = fit_logreg_ext(train, test, ecols).fillna(0).values
            ranks = sim.groupby(["date", "tims"])[["hgb_e", "lgbm_e", "logreg_e"]].rank(pct=True)
            sim["ensemble_e"] = ranks.mean(axis=1)
            picks_by_model.setdefault("ensemble_e", []).append(picks_of(sim, "ensemble_e"))
            aucs.setdefault("ensemble_e", []).append(
                roc_auc(test["scored"].astype(int), sim["ensemble_e"]))

        for method in list(picks_by_model):
            picks = picks_by_model[method][-1]
            acc = 100 * int((picks["scored"] == 1).sum()) / len(picks) if len(picks) else np.nan
            fold_acc.setdefault(method, []).append(acc)
        print(f"  fold {fold_no}: sim rows {len(sim):,}, elapsed {time.time() - t0:.0f}s, "
              f"fold acc: " + " ".join(f"{m}={fold_acc[m][-1]:.1f}" for m in picks_by_model),
              flush=True)

        if args.save_oof is not None:
            cols = ["date", "player_id", "name", "team_name", "home", "tims", "scored",
                    "gpg", "hgpg", "lgbm", "lgbm_e"] + ecols
            if use_ensemble:
                cols += ["hgb_e", "logreg_e", "ensemble_e"]
            sims.append(sim[cols].assign(fold=fold_no))

    pooled = {m: pd.concat(v, ignore_index=True) for m, v in picks_by_model.items()}

    print("\n=== factor meta ===")
    print(pd.DataFrame(embed_meta).round(4).to_string(index=False))

    print(f"\n=== per-fold accuracy (%), seed {args.seed} ===")
    n_done = len(next(iter(fold_acc.values())))
    stats = pd.DataFrame(
        [{"fold": i + 1, **{m: fold_acc[m][i] for m in fold_acc}} for i in range(n_done)]
    ).set_index("fold")
    print(stats.round(2).to_string())
    summary = stats.agg(["mean", "std"]).T
    summary["pooled_hits"] = [(pooled[m]["scored"] == 1).sum() for m in summary.index]
    summary["pooled_total"] = [len(pooled[m]) for m in summary.index]
    summary["pooled_acc"] = 100 * summary["pooled_hits"] / summary["pooled_total"]
    print("\nsummary (mean +/- std over folds; pooled = all folds):")
    print(summary.round(3).to_string())

    print("\n=== held-out AUC (mean over folds) ===")
    for m, v in aucs.items():
        print(f"  {m:12s} {np.mean(v):.4f} +/- {np.std(v):.4f}")

    print("\n=== per-group hit % (g1/g2/g3) and eras ===")
    for m, p in pooled.items():
        g_txt = " ".join(
            f"{100 * (p[p['tims'] == g]['scored'] == 1).sum() / max((p['tims'] == g).sum(), 1):.1f}"
            for g in (1, 2, 3)
        )
        seasons = np.array([season_start_year(d) for d in p["date"]])
        y23 = p[seasons >= 2023]
        y25 = p[seasons == 2025]
        print(f"  {m:12s} {g_txt} | 2023-24+: {(y23['scored'] == 1).sum()}/{len(y23)} "
              f"= {100 * (y23['scored'] == 1).sum() / max(len(y23), 1):.3f}% | "
              f"2025-26: {(y25['scored'] == 1).sum()}/{len(y25)} "
              f"= {100 * (y25['scored'] == 1).sum() / max(len(y25), 1):.3f}%")

    print("\n=== McNemar exact, pooled paired picks ===")
    for m in pooled:
        if not m.endswith("_e"):
            continue
        same_run_base = m.replace("_e", "")
        if same_run_base in pooled:
            n10, n01, p = mcnemar(pooled[m], pooled[same_run_base])
            print(f"  {m} vs {same_run_base} (same run): {n10}-{n01}, p={p:.4f}")
        n10, n01, p = mcnemar(pooled[m], pooled["hgpg"])
        print(f"  {m} vs hgpg: {n10}-{n01}, p={p:.4f}")
        n10, n01, p = mcnemar(pooled[m], pooled["gpg"])
        print(f"  {m} vs gpg: {n10}-{n01}, p={p:.4f}")
        if m == "lgbm_e" and ref_lgbm is not None:
            n10, n01, p = mcnemar(pooled[m], ref_lgbm)
            print(f"  {m} vs reference LGBM file: {n10}-{n01}, p={p:.4f}")
        if m == "lgbm" and ref_lgbm is not None:
            n10, n01, p = mcnemar(pooled[m], ref_lgbm)
            print(f"  {m} vs reference LGBM file (cache-era drift): {n10}-{n01}, p={p:.4f}")
        if m == "ensemble_e" and ref_ens is not None:
            n10, n01, p = mcnemar(pooled[m], ref_ens)
            print(f"  {m} vs reference ensemble file: {n10}-{n01}, p={p:.4f}")

    if args.save_oof is not None and sims:
        args.save_oof.parent.mkdir(parents=True, exist_ok=True)
        out = pd.concat(sims, ignore_index=True)
        out.to_csv(args.save_oof, index=False)
        print(f"\nsaved {len(out):,} OOF rows to {args.save_oof}")
    print(f"\ntotal wall time: {time.time() - t0:.0f}s")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env", default="dev")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--splits", type=int, default=5)
    parser.add_argument("--max-folds", type=int, default=0,
                        help="stop after this many folds (0 = all); smoke tests use 1")
    parser.add_argument("--embed-dim", type=int, default=3, help="ALS rank (2-5)")
    parser.add_argument("--als-reg", type=float, default=10.0, help="ridge lambda for the ALS solves")
    parser.add_argument("--als-iters", type=int, default=15, help="ALS sweeps")
    parser.add_argument(
        "--models",
        default="lgbm",
        help="comma list from {lgbm,ensemble}; the no-factor lgbm baseline always fits",
    )
    parser.add_argument("--save-oof", type=Path, default=None)
    args = parser.parse_args()
    args.models = [m.strip() for m in args.models.split(",") if m.strip()]
    run(args)


if __name__ == "__main__":
    main()
