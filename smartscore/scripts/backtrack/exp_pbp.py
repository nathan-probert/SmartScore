"""V16-pbp — pass-context first-party per-shot xG proxy, evaluated on picks.

Replaces the geometry-only per-shot xG (V8-own-xG, OOF AUC 0.769, no pick
gain) with a possession-context proxy built from the NHL gamecenter
play-by-play feed, aggregated by fetch_pbp.py into data/ml/pbp_shots.csv and
pbp_player_game.csv. The feed carries no pass events, so pass precedence is
proxied by raw event adjacency: prior_own_events (own-team events in the
current possession chain), one_touch_raw (first own event of the chain =
the one-touch vs carry-in split), rebound_raw (immediately preceding own-team
shot event) and prior_event_last_dx/dy (shot point minus the last own-team
coord event = pass/carry origin). All first-party, raw attributes only.

Pipeline (model.py machinery; model.py NOT edited):

  1. OOF per-shot xG, leak-free per fold like V8-own-xG: the challenge dates
     are cut into five shuffled date blocks with model.fold_blocks(seed);
     every other date carrying shots additionally gets five blocks. For each
     block, a per-shot LGBMClassifier trains on all shots whose date lies
     outside the block (asserted) and predicts the block's shots. Two
     variants per block:
       geo-only  - distance/angle/HD/slot/shot type/rebound/PP/empty net
       enriched  - geo-only + possession block (prior_own_events,
                   one_touch_raw, prior dx/dy, cross-ice flag)
     The enriched per-shot AUC is the "did pass-context move the proxy"
     headline vs the 0.769 geometry-only reference.
  2. Player features. Enriched per-game xG sums -> strictly prior-game
     rolling means over covered (PBP-fetched) games, min-coverage guarded:
       rs_pbxg_pg15/30   mean xG per covered game, prior 15/30 (>=10/20.)
       rs_pbxg_pa15      sum xG / sum attempts
       rs_pbprior_share15/30, rs_pbone_touch_share15, rs_pbrebound_share15,
       rs_pbhd_share15, rs_pbcross_share15, rs_pbslot_share15, rs_pbpp_share15
     Every window is shift(1); a perturbation check asserts the current
     game's own xG never enters its own features. Partial PBP coverage is
     handled by a missing indicator + 0.0-imputation on every rolling
     feature (_miss companion columns), so rows without enough covered
     history stay valid.
  3. Player model. SAME k=5 challenge-date k-fold, --train-subset all, LGBM
     config a (the V7/V8 gate). --with-pbp adds the feature block; the
     reference runs identical folds without it. Exact McNemar (binomtest)
     vs gpg, hgpg and the reference LGBM on pooled paired picks, plus the
     same tests against the saved baseline OOF files (oof_v6_baseline /
     oof_v7_base_lgbm / oof_v6c_ensemble seed files).

Usage:
  OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
      smartscore/scripts/backtrack/exp_pbp.py --env dev --seed 1 --with-pbp
  (reference arm: same command WITHOUT --with-pbp)
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

from lightgbm import LGBMClassifier  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402

import model  # noqa: E402
from model import (  # noqa: E402
    add_derived,
    build_frame,
    daily_picks,
    fold_blocks,
    load_frames,
    score_picks,
)

CACHE_DIR = model.CACHE_DIR  # data/ml

SHOT_LGBM = dict(
    n_estimators=400,
    learning_rate=0.05,
    num_leaves=31,
    min_child_samples=100,
)

# Per-shot feature blocks (all raw attributes off the PBP feed).
GEO_COLS = ["dist", "angle"]
GEO_FLAGS = ["rebound_raw", "pp_flag", "empty_net", "period"]
POSS_COLS = ["prior_own_events", "one_touch_raw", "abs_dx", "abs_dy", "cross_ice_flag"]

# Rolling per-player features merged onto the frame.
PBX_FEATURES = [
    "rs_pbxg_pg15",
    "rs_pbxg_pg30",
    "rs_pbxg_pa15",
    "rs_pbprior_share15",
    "rs_pbprior_share30",
    "rs_pbone_touch_share15",
    "rs_pbrebound_share15",
    "rs_pbhd_share15",
    "rs_pbcross_share15",
    "rs_pbslot_share15",
    "rs_pbpp_share15",
]

# Missing-indicator companion for every rolling feature: 1.0 where the
# rolling value was imputed (no PBP row for that player on that date, or a
# window that failed the min-coverage guard), 0.0 where genuinely measured.
PBX_MISS = [f"{name}_miss" for name in PBX_FEATURES]

REF_OOF_DIR = Path(__file__).resolve().parents[3] / "data" / "ml"
REF_FILES = {
    # exact paired references; missing files are reported and skipped
    "lgbm": "oof_v7_base_lgbm_seed{seed}.csv",
    "ensemble": "oof_v6c_ensemble_seed{seed}.csv",
    "gpg": "oof_v6_baseline_seed{seed}.csv",
    "hgpg": "oof_v6_baseline_seed{seed}.csv",
}


def load_pbp_tables():
    """The two aggregate tables; no df edits elsewhere - read-only adoption."""
    shots_path = CACHE_DIR / "pbp_shots.csv"
    players_path = CACHE_DIR / "pbp_player_game.csv"
    if not shots_path.exists() or not players_path.exists():
        raise RuntimeError("pbp tables missing - run fetch_pbp.py first")

    shots = pd.read_csv(shots_path)
    players = pd.read_csv(players_path)

    shots["x"] = pd.to_numeric(shots["x"], errors="coerce")
    shots["y"] = pd.to_numeric(shots["y"], errors="coerce")
    shots["dist"] = pd.to_numeric(shots["dist"], errors="coerce")
    shots["angle"] = pd.to_numeric(shots["angle"], errors="coerce")
    shots["hd_flag"] = shots["dist"] < 20.0
    shots["slot_flag"] = (
        shots["x"].abs().between(54.0, 89.0) & (shots["y"].abs() <= 22.0)
    )
    shots["period"] = pd.to_numeric(shots["period"], errors="coerce")
    shots["abs_dx"] = pd.to_numeric(shots["prior_event_last_dx"], errors="coerce").abs()
    shots["abs_dy"] = pd.to_numeric(shots["prior_event_last_dy"], errors="coerce").abs()
    shots["cross_ice_flag"] = shots["abs_dy"].ge(25.0).astype(float)
    shots["prior_own_events"] = pd.to_numeric(shots["prior_own_events"], errors="coerce")

    for t in (shots, players):
        # CSV round-trips parse the season id as int64; keep it text.
        t["season"] = t["season"].astype("Int64").astype(str)
        t["date"] = t["game_date"].astype(str)
        t["player_id"] = t["player_id"].astype("int64")
        t["game_id"] = t["game_id"].astype("int64")
    return shots, players


def shot_design(shots, enriched):
    parts = [
        shots[GEO_COLS],
        shots[GEO_FLAGS],
        pd.get_dummies(shots["shot_type"], prefix="st").astype(float),
        shots["hd_flag"].astype(float),
        shots["slot_flag"].astype(float),
    ]
    if enriched:
        parts.append(shots[POSS_COLS])
    return pd.concat(parts, axis=1)


def add_player_features(players, shots, perturb=None):
    """xG-per-game + strictly prior rolling means over covered games.

    ``perturb=(player_id, game_id)`` for the leak check only: that one
    per-shot xG sum is set to 99 BEFORE any rolling window is built, so the
    assertion reads "the same game's own features are untouched by its own
    xG".
    """
    per_game = shots.groupby(["game_id", "player_id"], as_index=False)["xg"].sum()
    per_game = per_game.rename(columns={"xg": "xg_pg"})
    if perturb is not None:
        p_pid, p_gid = perturb
        pmask = (per_game["player_id"] == p_pid) & (per_game["game_id"] == p_gid)
        if pmask.any():
            per_game.loc[pmask, "xg_pg"] = 99.0
    # multi-shot player-games also exist in `players` (n_att>0) - one row each
    tab = players.merge(per_game, on=["game_id", "player_id"], how="left", validate="many_to_one")
    tab["xg_pg"] = tab["xg_pg"].fillna(0.0)

    tab = tab.sort_values(["player_id", "date", "game_id"]).reset_index(drop=True)
    group = tab.groupby("player_id", sort=False)

    for window, min_cov in ((15, 10), (30, 20)):
        tab[f"rs_pbxg_pg{window}"] = group["xg_pg"].transform(
            lambda s: s.shift(1).rolling(window, min_periods=min_cov).mean()
        )
    xg_sum = group["xg_pg"].transform(lambda s: s.shift(1).rolling(15, min_periods=10).sum())
    att_sum = group["n_att"].transform(lambda s: s.shift(1).rolling(15, min_periods=10).sum())
    tab["rs_pbxg_pa15"] = xg_sum / att_sum.replace(0, np.nan)

    def _rolling_share(series, window):
        """mean over the window of share-like per-game columns, prior only."""
        return series.shift(1).rolling(window, min_periods=10).mean()

    # rate columns per player-game share, then prior-window means
    tab["prior_rate"] = (tab["n_prior"] / tab["n_att"].replace(0, np.nan)).astype(float)
    tab["one_touch_rate"] = (tab["n_one_touch"] / tab["n_att"].replace(0, np.nan)).astype(float)
    tab["rebound_rate"] = (tab["n_rebound"] / tab["n_att"].replace(0, np.nan)).astype(float)
    tab["hd_rate"] = (tab["n_hd"] / tab["n_att"].replace(0, np.nan)).astype(float)
    tab["cross_rate"] = (tab["n_cross_ice"] / tab["n_att"].replace(0, np.nan)).astype(float)
    tab["slot_rate"] = (tab["n_slot"] / tab["n_att"].replace(0, np.nan)).astype(float)
    tab["pp_rate"] = (tab["n_pp"] / tab["n_att"].replace(0, np.nan)).astype(float)

    group = tab.groupby("player_id", sort=False)
    tab["rs_pbprior_share15"] = group["prior_rate"].transform(lambda s: s.shift(1).rolling(15, min_periods=10).mean())
    tab["rs_pbprior_share30"] = group["prior_rate"].transform(lambda s: s.shift(1).rolling(30, min_periods=20).mean())
    tab["rs_pbone_touch_share15"] = group["one_touch_rate"].transform(lambda s: s.shift(1).rolling(15, min_periods=10).mean())
    tab["rs_pbrebound_share15"] = group["rebound_rate"].transform(lambda s: s.shift(1).rolling(15, min_periods=10).mean())
    tab["rs_pbhd_share15"] = group["hd_rate"].transform(lambda s: s.shift(1).rolling(15, min_periods=10).mean())
    tab["rs_pbcross_share15"] = group["cross_rate"].transform(lambda s: s.shift(1).rolling(15, min_periods=10).mean())
    tab["rs_pbslot_share15"] = group["slot_rate"].transform(lambda s: s.shift(1).rolling(15, min_periods=10).mean())
    tab["rs_pbpp_share15"] = group["pp_rate"].transform(lambda s: s.shift(1).rolling(15, min_periods=10).mean())

    return tab


def cover_missing(tab):
    """Missing-indicator + imputation so partial PBP coverage stays valid.

    A player-game with no PBP row, or a window shorter than the min-coverage
    guard, gets _miss=1 and the rolling value 0.0 (a constant the models can
    treat like 'no history', identified apart from a real measurement by the
    indicator). Deterministic and leak-free: no data-derived constant.
    """
    tab = tab.copy()
    for name in PBX_FEATURES:
        tab[f"{name}_miss"] = tab[name].isna().astype(float)
        tab[name] = tab[name].fillna(0.0)
    return tab



def paired_counts(variant_picks, baseline_picks):
    """Exact McNemar on pooled picks, paired by (date, tims)."""
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


def summarize(arm, picks, fold_accs):
    hits = int((picks["scored"] == 1).sum())
    total = len(picks)
    p23 = picks[picks["date"] >= "2023-08-01"]
    h2324, n2324 = int((p23["scored"] == 1).sum()), len(p23)
    p25 = picks[picks["date"] >= "2025-08-01"]
    h2526, n2526 = int((p25["scored"] == 1).sum()), len(p25)
    groups = picks.groupby("tims")["scored"].agg(["count", "sum"])
    g_txt = " ".join(f"g{int(t)}={100 * row['sum'] / row['count']:.1f}" for t, row in groups.iterrows())
    return {
        "arm": arm,
        "hits": hits,
        "total": total,
        "acc": 100 * hits / total if total else np.nan,
        "fold_mean": float(np.mean(fold_accs)),
        "fold_std": float(np.std(fold_accs)),
        "groups": g_txt,
        "hit2324": f"{h2324}/{n2324}={100 * h2324 / n2324:.2f}%" if n2324 else "-",
        "hit2526": f"{h2526}/{n2526}={100 * h2526 / n2526:.2f}%" if n2526 else "-",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--splits", type=int, default=5)
    parser.add_argument("--train-subset", choices=["challenge", "all"], default="all")
    parser.add_argument("--model", choices=["lgbm", "ensemble"], default="lgbm",
                        help="gate model (lgbm, default) or full ensemble")
    parser.add_argument("--with-pbp", action="store_true", help="add the V16-pbp rolling features")
    parser.add_argument("--oof-file", type=Path, default=None, help="save pooled OOF pick rows here")
    parser.add_argument("--smoke", action="store_true", help="first ~120 challenge dates, 30% train")
    args = parser.parse_args()

    t0 = time.time()
    shots = players_tab = None
    if args.with_pbp:
        shots, players_tab = load_pbp_tables()
        players_tab = players_tab.sort_values(["player_id", "date", "game_id"]).reset_index(drop=True)
        shots = shots.sort_values(["date", "game_id", "event_id"]).reset_index(drop=True)

    players, teams, goalies = load_frames(args.env)
    frame = build_frame(players, teams, goalies)
    frame = add_derived(frame, teams)
    n0 = len(frame)
    print(f"frame built: {len(frame):,} rows in {time.time() - t0:.0f}s", flush=True)

    challenge = frame["tims"].isin([1, 2, 3]) & frame["scored"].notna()
    challenge_dates = sorted(frame.loc[challenge, "date"].unique())
    if args.smoke:
        challenge_dates = challenge_dates[:120]
    blocks = fold_blocks(challenge_dates, args.splits, args.seed)

    shot_aucs = None
    if args.with_pbp:
        # ---- per-shot OOF xG: challenge blocks + five extra blocks for the
        # non-challenge dates that carry shots ----
        nonchallenge = sorted(set(shots["date"]) - set(challenge_dates))
        block_lists = (
            [list(b) for b in fold_blocks(challenge_dates, args.splits, args.seed)]
            + [list(b) for b in fold_blocks(nonchallenge, args.splits, args.seed)]
        )
        if args.smoke:
            allowed = set(challenge_dates)
            shots = shots[shots["date"].isin(allowed)].reset_index(drop=True)
            block_lists = [b for b in block_lists if set(b) & allowed]

        labels = shots["is_goal"].astype(int).to_numpy()
        shot_dates = shots["date"].to_numpy()
        design_geo = shot_design(shots, enriched=False)
        design_rich = shot_design(shots, enriched=True)
        xg_oof = np.full(len(shots), np.nan)
        aucs_geo, aucs_rich = [], []

        for i, block_dates in enumerate(block_lists, start=1):
            test_mask = np.isin(shot_dates, block_dates)
            if not test_mask.any():
                continue
            assert not (set(shot_dates[~test_mask]) & set(block_dates)), "fold overlap"
            for tag, design in (("geo", design_geo), ("rich", design_rich)):
                classifier = LGBMClassifier(
                    objective="binary", subsample=0.8, colsample_bytree=0.8,
                    random_state=0, n_jobs=int(os.environ.get("LGBM_N_JOBS", 4)),
                    verbose=-1, **SHOT_LGBM,
                )
                classifier.fit(design[~test_mask], labels[~test_mask])
                prob = classifier.predict_proba(design[test_mask])[:, 1]
                if tag == "geo":
                    aucs_geo.append(roc_auc_score(labels[test_mask], prob))
                else:
                    aucs_rich.append(roc_auc_score(labels[test_mask], prob))
                    xg_oof[np.where(test_mask)[0]] = prob
            print(f"  shot-xg block {i}/{len(block_lists)}: geo {aucs_geo[-1]:.4f} rich {aucs_rich[-1]:.4f}", flush=True)

        shots["xg"] = xg_oof
        pd_tab = add_player_features(players_tab, shots)
        pd_tab = cover_missing(pd_tab)

        # strict prior-game assertion: perturbing a current game's xG must
        # not move ANY of its own rolling features.
        idx0 = int((pd_tab["n_att"] > 0).idxmax())
        pid0, gid0 = pd_tab.loc[idx0, ["player_id", "game_id"]]
        before = pd_tab.loc[idx0, PBX_FEATURES].astype(float)
        after_tab = cover_missing(add_player_features(players_tab, shots, perturb=(pid0, gid0)))
        mask_np = ((after_tab["player_id"] == pid0) & (after_tab["game_id"] == gid0)).to_numpy()
        j = int(mask_np.argmax())
        after = after_tab.loc[j, PBX_FEATURES].astype(float)
        assert np.allclose(before.to_numpy(), after.to_numpy()), "same-game leak in rolling features"
        print("leak check passed (current game xG perturbation)", flush=True)

        merged = frame.merge(
            pd_tab[["date", "player_id"] + PBX_FEATURES + PBX_MISS],
            on=["date", "player_id"], how="left", validate="many_to_one",
        )
        assert len(merged) == n0 and not [c for c in merged.columns if str(c).endswith(("_x", "_y"))]
        # Frame rows with no pbp tab row at all: mark miss=1 and impute 0.0,
        # the same treatment cover_missing applies inside the tab.
        for name, miss in zip(PBX_FEATURES, PBX_MISS):
            merged[miss] = merged[miss].fillna(1.0)
            merged[name] = merged[name].fillna(0.0)
        frame = merged

        model.NUMERIC_FEATURES = model.NUMERIC_FEATURES + PBX_FEATURES + PBX_MISS
        model.DATASET_FEATURES = model.NUMERIC_FEATURES + model.CATEGORICAL_FEATURES
        shot_aucs = (
            f"geo-only {np.mean(aucs_geo):.4f} +- {np.std(aucs_geo):.4f}; "
            f"enriched {np.mean(aucs_rich):.4f} +- {np.std(aucs_rich):.4f}"
        )
        # 'Covered' now means measured (not imputed): the miss indicator must
        # say which challenge rows genuinely carry PBP history.
        cov_name, miss_name = "rs_pbxg_pg15", "rs_pbxg_pg15_miss"
        cov = int((frame[miss_name] == 0).sum())
        cov_ch = int((challenge & (frame[miss_name] == 0)).sum())
        print(
            f"pbp features: {cov:,} frame rows / {cov_ch:,} challenge rows MEASURED "
            f"({100 - 100 * float(frame[miss_name].mean()):.2f}% / "
            f"{100 - 100 * float(frame.loc[challenge, miss_name].mean()):.2f}%); "
            f"{len(shots):,} shots ({int(shots['is_goal'].sum()):,} goals); per-shot AUC {shot_aucs}",
            flush=True,
        )

    # ---------- player folds: the SAME k=5 date k-fold every run ----------
    method_names = ["gpg", "hgpg", "lgbm"] + (
        ["hgb", "logreg", "ensemble"] if args.model == "ensemble" else []
    )
    pooled = {m: [] for m in method_names}
    fold_accs = {m: [] for m in method_names}
    oof_rows = []

    for number, block_dates in enumerate(blocks, start=1):
        tf = time.time()
        test_dates = pd.Index(block_dates)
        test = frame[challenge & frame["date"].isin(test_dates)].copy()
        train_all = frame[~frame["date"].isin(test_dates) & frame["scored"].notna()]
        if args.train_subset == "challenge":
            train_all = train_all[train_all["tims"].isin([1, 2, 3])]
        if args.smoke:
            train_all = train_all.sample(frac=0.3, random_state=0)

        sim = test.copy()
        for tag, col in (("gpg", "gpg"), ("hgpg", "hgpg")):
            sim[tag] = test[col].astype(float).fillna(-np.inf)
            picks = daily_picks(sim, tag)
            pooled[tag].append(picks)
            fold_accs[tag].append(score_picks(picks, tag)["acc"])

        if args.model == "ensemble":
            sim["hgb"] = model.fit_hgb(train_all, test).fillna(0).values
            sim["logreg"] = model.fit_logreg(train_all, test).fillna(0).values
            sim["lgbm"] = model.fit_lgbm(train_all, test).fillna(0).values
            ranks = sim.groupby(["date", "tims"])[["hgb", "lgbm", "logreg"]].rank(pct=True)
            sim["ensemble"] = ranks.mean(axis=1)
            for method in ("hgb", "logreg", "lgbm", "ensemble"):
                picks = daily_picks(sim, method)
                pooled[method].append(picks)
                fold_accs[method].append(score_picks(picks, method)["acc"])
        else:
            sim["lgbm"] = model.fit_lgbm(train_all, test).fillna(0).values
            picks = daily_picks(sim, "lgbm")
            pooled["lgbm"].append(picks)
            fold_accs["lgbm"].append(score_picks(picks, "lgbm")["acc"])

        keep = ["date", "player_id", "tims", "scored", "gpg", "hgpg", "lgbm"]
        if args.model == "ensemble":
            keep += ["hgb", "logreg", "ensemble"]
        oof_rows.append(sim[keep].assign(fold=number))
        print(f"  fold {number}: {len(sim):,} sim rows ({time.time() - tf:.0f}s)", flush=True)

    pooled_picks = {m: pd.concat(parts, ignore_index=True) for m, parts in pooled.items()}

    print("\n=== pooled out-of-fold picks ===")
    for arm in method_names:
        row = summarize(arm, pooled_picks[arm], fold_accs[arm])
        print(
            f"{row['arm']:8s} {row['hits']}/{row['total']} = {row['acc']:.3f}% "
            f"(folds {row['fold_mean']:.3f} +- {row['fold_std']:.3f}) | {row['groups']} | "
            f"2023-24+ {row['hit2324']} | 2025-26 {row['hit2526']}"
        )

    print("\n=== exact McNemar (pooled paired picks) ===")
    target = "lgbm" if args.model == "lgbm" else "ensemble"
    for ref in ("gpg", "hgpg", "lgbm"):
        if ref == target:
            continue
        a, b, p = paired_counts(pooled_picks[target], pooled_picks[ref])
        m2 = pooled_picks[target][["date", "tims", "player_id"]].merge(
            pooled_picks[ref][["date", "tims", "player_id"]], on=["date", "tims"], suffixes=("_v", "_b")
        )
        changed = int((m2["player_id_v"] != m2["player_id_b"]).sum())
        print(f"{target:8s} vs {ref:5s}: {a}-{b}, p={p:.4f} (changed picks {changed})")

    # Same tests against the SAVED baseline/reference OOF pick files (paired
    # on (date, tims)): gpg/hgpg from oof_v6_baseline, lgbm from
    # oof_v7_base_lgbm, ensemble from oof_v6c_ensemble.
    print(f"\n=== exact McNemar vs saved OOF files (seed {args.seed}) ===")
    for ref in ("gpg", "hgpg", "lgbm", "ensemble"):
        if ref == target:
            continue
        ref_path = REF_OOF_DIR / REF_FILES[ref].format(seed=args.seed)
        if not ref_path.exists():
            print(f"{target:8s} vs {ref:5s}: reference file missing ({ref_path.name}) - skipped")
            continue
        ref_picks = daily_picks(pd.read_csv(ref_path), ref)
        if len(ref_picks) == 0:
            print(f"{target:8s} vs {ref:5s}: no reference picks parsed - skipped")
            continue
        same_side = int((ref_picks["scored"] == 1).sum())
        a, b, p = paired_counts(pooled_picks[target], ref_picks)
        print(
            f"{target:8s} vs {ref:5s}: {a}-{b}, p={p:.4f} "
            f"(file ref {len(ref_picks):,} picks, {100 * same_side / len(ref_picks):.3f}%)"
        )

    if args.oof_file:
        args.oof_file.parent.mkdir(parents=True, exist_ok=True)
        pd.concat(oof_rows, ignore_index=True).to_csv(args.oof_file, index=False)
        print(f"saved OOF rows -> {args.oof_file}")
    print(f"total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
