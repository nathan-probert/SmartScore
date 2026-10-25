"""On-demand, LOCAL training of the SmartScore ensemble -> ONE export artifact.

GOAL (owner rules, binding)

Training NEVER runs on AWS. The owner runs this script locally on demand, it
produces a versioned artifact, and (with --upload) puts that artifact on S3
for the predict-only Lambda (MLPicks-{ENV}). Nothing in this file is
scheduled; nothing daily runs locally either - this is an owner-invoked,
one-shot command.

RAMBLE FREE / NO FORKING

Every hyperparameter lives in ``smartscore/scripts/backtrack/model.py``
(fit_hgb, fit_lgbm, fit_logreg, fit_ranker pins). This script imports model.py
math verbatim and adds one thing only: the deployment mode, where ALL scored
rows through --through-date are ONE training fold (no held-out test, no fold
sim), then exports everything a predict-only inference process needs.

SEED + MEASURED NUMBERS

The fitted estimators are always pinned random_state=0 inside model.py;
--seed only drives the k-fold date shuffle when --oof measures THIS bundle.
Canonical deployment seed is 0 - the best of the repo's pooled out-of-fold
sweep (pooled OOF pick accuracy, one pick per tims group per day):
seed 0 = 29.92%, seed 1 = 29.41%, seed 3 = 29.82% (96f0ff2 cache/MODEL_NOTES.md).
The table is recorded in the artifact metadata so any vintage traces to a
measured expected accuracy. Regenerating the ensemble on the current cache
(617,310 rows incl. the V15 2013-17 rows) measured seeds 1/3 at 29.28/29.82%;
era table lives in the runbook (aws-predict-lambda-runbook.md).

ARTIFACT

``data/ml/artifact_<train_through>_<seed>.joblib`` (zlib-compressed joblib),
emitting BOTH agreed designs at once (the Lambda's normalise_bundle reads
either; joblib dedupes the shared estimator references):

    version                      1
    train_through / seed
    git_hash / generated_at
    feature_order                the EXACT ordered feature list the frame must
                                 be rebuilt in (the Lambda asserts this)
    categorical_maps             {col: [values]} per CATEGORICAL_FEATURE -
                                 the trained vocabulary the Lambda's encoding
                                 step needs
    derived_params               the documented ensemble scoring recipe:
                                 mean of within-(date, tims) rank(pct=True)
                                 across {hgb, lgbm, logreg}, fillna(0) before
                                 ranking, ranker excluded, daily_picks takes
                                 the per-group max
    models                       {hgb, lgbm, logreg, ranker} fitted estimators

    legacy aliases (same objects): hgb, lgbm, logreg, ranker,
    numeric_features / categorical_features / all_features, categories,
    required_snapshot_columns (the 16 Supabase columns the frame reads,
    docs/supabase_shot_columns.md), metadata (train_through, seed, git hash,
    rows, dates, fit seconds, library versions, expected OOF accuracy table)

USAGE (owner-run, from the repo root; .env supplies Supabase config when
--source supabase; sqlite uses data/raw_nhl.sqlite)

    uv run python smartscore/scripts/backtrack/train_export.py \
        [--through-date YYYY-MM-DD] [--seed 0] [--env dev]
        [--source sqlite|supabase] [--oof] [--upload]
        [--s3-bucket ML_S3_BUCKET] [--s3-prefix predict/ml]

--upload uploads via boto3 bucket/prefix from --s3-bucket/--s3-prefix (or the
ML_S3_BUCKET / ML_S3_PREFIX environment variables). DEFAULT IS NO UPLOAD - an
owner-run by itself never touches AWS.
"""

import argparse
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "smartscore"))
sys.path.insert(1, Path(__file__).resolve().parent)

import joblib  # noqa: E402
import model  # noqa: E402

# noqa: E402
import pandas as pd  # noqa: E402 (model.py uses the global pd namespace)

CACHE_DIR = ROOT / "data" / "ml"

# Pooled out-of-fold 3-picks-per-day accuracy by seed (96f0ff2 cache,
# MODEL_NOTES.md). Quoted - never tuned - by the deployment number table.
EXPECTED_OOF = {0: 29.92, 1: 29.41, 3: 29.82}
DEPLOYMENT_SEED = 0
LGBM_CONFIG = "a"


def git_commit():
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True  # noqa: S607
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def load_frame(args):
    """The through-date frame via model.py's own builders - no fork."""
    print(f"loading frames (env={args.env}, source={args.source}) ...", flush=True)
    players, teams, goalies = model.load_frames(args.env, refresh=args.refresh)
    frame = model.build_frame(players, teams, goalies, source=args.source)
    frame = model.add_derived(frame, teams, source=args.source)
    frame = frame[frame["scored"].notna()].reset_index(drop=True)
    if args.through_date:
        frame = frame[frame["date"].astype(str) <= args.through_date].reset_index(drop=True)
    print(f"training rows: {len(frame):,} through {frame['date'].max()}", flush=True)
    return frame


def fit_bundle(frame, args):
    """Fit on ONE fold that is the whole scored frame (all through-date rows).

    model.fit_hgb / fit_lgbm / fit_logreg / fit_ranker all take (train, test)
    and fit on train; a one-row test frame keeps the calls verbatim while
    making the returned in-fold predictions degenerate (discarded). Encoding
    exactly mirrors model.categorise(frame, <empty>): category vocabulary from
    the training columns only, strings to category dtype.
    """
    probe = frame.iloc[:1]  # a one-row "test"; the 0-row shape trips sklearn
    x_train, _ = model.categorise(frame, probe)
    timings = {}

    # model.fit_* fit AND predict, returning a probability Series - the fitted
    # estimator itself is not exposed. A capture wrapper on the estimator
    # classes (set back after fitting) grabs the fitted objects WITHOUT
    # touching any hyperparameter: construction stays 100% inside model.py.
    captured = {}
    cap_stack = []
    import sklearn.ensemble  # noqa: E401, PLC0415, F401  (pre-seeds the modules we wrapper-capture)
    import sklearn.linear_model  # noqa: E401, PLC0415, F401
    try:
        import lightgbm  # noqa:PLC0415  (optional dep; ranked bundle member)
    except ImportError:
        lightgbm = None  # ranker ships omitted; a warning comes from fit_bundle
    def _wrap(holder, attr):
        """Install a self-capturing subclass of holder.<attr>; restored later."""
        base = getattr(holder, attr)

        class _Captured(base):  # noqa: N801
            def fit(self, *fit_args, **fit_kwargs):
                result = super().fit(*fit_args, **fit_kwargs)
                captured[attr] = self
                return result

        setattr(holder, attr, _Captured)
        cap_stack.append((holder, attr, base))

    for module, attr in (
        (model, "HistGradientBoostingClassifier"),
        (lightgbm, "LGBMClassifier"),
        (lightgbm, "LGBMRanker"),
        (model, "LogisticRegression"),  # model.py binds it from sklearn.linear_model
    ):
        if module is not None and hasattr(module, attr):
            _wrap(module, attr)

    try:
        start = time.time()
        model.fit_hgb(frame, probe)  # probability output discarded
        timings["hgb_s"] = round(time.time() - start, 1)

        start = time.time()
        model.fit_lgbm(frame, probe, args.lgbm_config)
        timings["lgbm_s"] = round(time.time() - start, 1)

        start = time.time()
        model.fit_logreg(frame, probe)
        timings["logreg_s"] = round(time.time() - start, 1)

        ranker = None
        try:
            model.fit_ranker(frame, probe, "lambdarank")
        except ImportError:
            ranker = captured.get("LGBMRanker")
        if ranker is None:
            print("ranker: lightgbm unavailable; bundle ships without it", flush=True)
        timings["ranker_s"] = round(time.time() - start, 1)
    finally:
        for holder, name, base in cap_stack:
            setattr(holder, name, base)

    # unlock states: restore original classes so the objects pickle
    _restored = {attr: base for holder, attr, base in cap_stack}
    for attr, estimator in captured.items():
        try:
            estimator.__class__ = _restored[attr]
        except TypeError:
            pass

    hgb = captured["HistGradientBoostingClassifier"]
    lgbm = captured.get("LGBMClassifier")
    logreg = captured["LogisticRegression"]

    bundle = {
        # --- ARTIFACT CONTRACT keys (sibling-agreed; the Lambda reads both
        # --- designs via normalise_bundle) ------------------------------
        "version": 1,
        "train_through": str(frame["date"].max()),
        "seed": args.seed,
        "git_hash": (g := git_commit()),
        "generated_at": (generated_at := datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")),
        "feature_order": list(model.DATASET_FEATURES),
        "categorical_maps": {
            col: sorted(str(value) for value in frame[col].dropna().astype(str).unique())
            for col in model.CATEGORICAL_FEATURES
        },
        # The documented ensemble scoring recipe, verbatim the Lambda must
        # reproduce: per model.run_folds' ensemble column - predict each
        # member, fillna(0), rank(pct=True) within (date, tims), MEAN across
        # the {hgb, lgbm, logreg} ranks (the ranker is exported for
        # traceability only, never in the ensemble mean), then daily_picks
        # takes each group's max - exactly one pick per tims group per date.
        "derived_params": {
            "ensemble_recipe": "mean of within-(date, tims) rank(pct=True) "
            "across members {hgb, lgbm, logreg}",
            "ensemble_members": ["hgb", "lgbm", "logreg"],
            "ranker_in_ensemble": False,
            "fillna_before_rank": 0.0,
            "logreg_features": list(model.NUMERIC_FEATURES),
            "pick_rule": "model.daily_picks: max ensemble value within each "
            "(date, tims in {1,2,3}); exactly one pick per group per date",
            "required_snapshot_columns": list(FRAME_SOURCE_COLUMNS[args.source]),
        },
        "models": {
            "hgb": hgb,
            "lgbm": lgbm,
            "logreg": logreg,
            "ranker": ranker,
        },
        # --- legacy envelope keys (same objects; the Lambda's legacy path
        # --- and any older reader keep working) --------------------------
        "hgb": hgb,
        "lgbm": lgbm,
        "logreg": logreg,
        "ranker": ranker,
        "numeric_features": list(model.NUMERIC_FEATURES),
        "categorical_features": list(model.CATEGORICAL_FEATURES),
        "all_features": list(model.DATASET_FEATURES),
        "categories": {
            col: sorted(str(value) for value in frame[col].dropna().astype(str).unique())
            for col in model.CATEGORICAL_FEATURES
        },
        "required_snapshot_columns": FRAME_SOURCE_COLUMNS[args.source],
        "metadata": {
            "train_through": str(frame["date"].max()),
            "seed": args.seed,
            "version": 1,
            "generated_at": generated_at,
            "expected_oof_pct": {str(k): v for k, v in EXPECTED_OOF.items()},
            "oof_measured": False,
            "git_hash": (g := git_commit()),
            "git_commit": g,
            "frame_source": args.source,
            "rows": int(len(frame)),
            "dates": int(frame["date"].nunique()),
            "fit_seconds": timings,
            "lgbm_config": args.lgbm_config,
            "lineup_mode": "none",
            "trained_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        },
    }
    return bundle


# The 16 shot/TOI + club columns the frame reads per env path
# (docs/supabase_shot_columns.md); sqlite covers them from the raw store.
FRAME_SOURCE_COLUMNS = {
    "supabase": [
        "sd_shots_pg", "sd_goals_pg", "sd_toi_pg", "sd_sh_pct",
        "r5_shots_pg", "r5_goals_pg", "r10_shots_pg", "r10_goals_pg",
        "r10_toi_pg", "ewm_goals", "ewm_shots", "career_games",
        "career_goals_pg", "career_shots_pg", "team_abbrev", "opponent_abbrev",
    ],
    "sqlite": [],
}


def run_oof(frame, args):
    """Measure THIS bundle seed's pooled OOF pick accuracy via model.run_folds.

    Slow by design (a full k-fold sim; seeds 1/3 measured ~29.28/29.82% on the
    current cache). Skipped by default; the metadata seed table is the quoted
    number meanwhile.
    """

    class _Args:  # model.run_folds expects the argparse surface of model.main()
        refresh = False
        env = args.env
        split = "kfold"
        splits = args.oof_splits
        test_start = model.TEST_START
        min_season = None
        seed = args.seed
        train_subset = "all"
        model_ = "ensemble"
        model_name = "ensemble"
        ranker = "none"
        lgbm_config = args.lgbm_config

    print(f"oof: {args.oof_splits}-fold sim at seed {args.seed} (slow) ...", flush=True)
    start = time.time()
    _, pooled, _, _ = model.run_folds(frame, _Args())
    picks = pd.concat(pooled["ensemble"], ignore_index=True)
    result = model.score_picks(picks, "ensemble_oof")
    print(
        f"oof pooled seed {args.seed}: {result['hits']}/{result['total']} "
        f"= {result['acc']:.2f}% over {result['days']} dates ({time.time() - start:.0f}s)",
        flush=True,
    )
    return result


def upload_artifact(path, args):
    import boto3  # noqa: PLC0415  (boto3 only loads when the owner asks for S3)

    bucket = args.s3_bucket or os.environ.get("ML_S3_BUCKET", "")
    prefix = args.s3_prefix or os.environ.get("ML_S3_PREFIX", "predict/ml")
    if not bucket:
        raise SystemExit("--upload needs --s3-bucket (or ML_S3_BUCKET env)")
    key = f"{prefix.rstrip('/')}/{path.name}"
    boto3.client("s3").upload_file(str(path), bucket, key)
    print(f"uploaded: s3://{bucket}/{key}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="dev", help="backtrack-table suffix (dev default locally; prod for deploy)")
    parser.add_argument("--refresh", action="store_true", help="re-download the table CSV caches")
    parser.add_argument("--source", choices=model.FRAME_SOURCES, default="sqlite",
                        help="sqlite (default, local raw store) or supabase (published 16 columns)")
    parser.add_argument("--through-date", default=None,
                        help="last training date YYYY-MM-DD (default: the store/cache's latest date)")
    parser.add_argument("--seed", type=int, default=DEPLOYMENT_SEED,
                        help="fold-shuffle seed for --oof + metadata (default 0 = the measured best: "
                        "29.92%% vs 29.41%% s1 / 29.82%% s3)")
    parser.add_argument("--oof", action="store_true",
                        help="run the k-fold sim after fitting (slow; measures this seed)")
    parser.add_argument("--oof-splits", type=int, default=5)
    parser.add_argument("--lgbm-config", choices=list(model.LGBM_CONFIGS), default=LGBM_CONFIG)
    parser.add_argument("--out", default=None,
                        help="override the artifact path (default data/ml/artifact_<through>_<seed>.joblib)")
    parser.add_argument("--upload", action="store_true", help="push the artifact to S3 (default: OFF)")
    parser.add_argument("--s3-bucket", default=os.environ.get("ML_S3_BUCKET", ""))
    parser.add_argument("--s3-prefix", default=os.environ.get("ML_S3_PREFIX", "predict/ml"))
    args = parser.parse_args()

    frame = load_frame(args)
    if frame.empty:
        raise SystemExit("no scored rows after filters; check --through-date / --env / the cache")

    bundle = fit_bundle(frame, args)
    bundle["metadata"]["oof_measured"] = args.oof
    path = Path(args.out) if args.out else CACHE_DIR / (
        f"artifact_{bundle['metadata']['train_through']}_{args.seed}.joblib")
    if str(path.parent) == str(CACHE_DIR):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, path, compress=("zlib", 3))
    print(f"artifact: {path} ({path.stat().st_size / 1_000_000:.1f} MB)", flush=True)
    print(f"metadata: {bundle['metadata']}", flush=True)

    if args.oof:
        result = run_oof(frame, args)
        bundle["metadata"]["oof_measured_acc_pct"] = round(float(result["acc"]), 2)
        joblib.dump(bundle, path, compress=("zlib", 3))

    if args.upload:
        upload_artifact(path, args)
    else:
        print("upload skipped (no --upload; owner rules: on-demand only)", flush=True)


if __name__ == "__main__":
    main()
