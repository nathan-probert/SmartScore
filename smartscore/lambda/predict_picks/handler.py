"""Predict-only ML picks Lambda: SmartScore ensemble -> Picks-{ENV}.

Owner rules this handler implements (docs/aws-predict-lambda-runbook.md):

  * training NEVER happens here (or anywhere on AWS) - the Lambda only loads
    a versioned joblib artifact from S3 that the owner's train_export.py run
    uploaded from the local machine;
  * it reads TODAY's rows read-only from the backtrack Supabase tables
    (Player-Snapshots / Team-Stats / Goalie-Snapshots) through
    ``model.build_frame(..., source="supabase")`` + ``model.add_derived`` -
    model.py's math is imported, never forked - requires the 16 shot/TOI +
    club columns (docs/supabase_shot_columns.md) with a hard fail naming
    exactly what is missing, and asserts the built frame has the artifact's
    exact ordered feature columns;
  * it picks one player per tims group (model.daily_picks' rule, applied by
    the ensemble's mean within-group rank percentile) and publishes through
    the existing Picks-{ENV} conventions (smartscore/utility.py save_to_db,
    reached via the publish_public_db row shape) - no manual Supabase writes.

Manual trigger: ``{"date": "YYYY-MM-DD"}`` overrides ET-today; ``dry: true``
skips the Picks write (smoke invoke).
"""

import datetime
import io
import json
import os
import sys
from pathlib import Path

import boto3
import numpy as np
import pandas as pd

# model.py (cherry-picked from feat/supabase-shot-cols) ships in the container
# at /var/task/frame/model.py; locally and in unit tests the repo's own copy
# under smartscore/scripts/backtrack is used - both resolve to the SAME code,
# this script never forks it.
_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent / "frame"))
sys.path.insert(1, str(_here.parents[2] / "scripts" / "backtrack"))
import model  # noqa: E402

from config import ENV, SUPABASE_ADMIN_CLIENT  # noqa: E402
from utility import save_to_db  # noqa: E402  (the existing Picks-{ENV} writer)

# Bounded prior window: prev2/3_goals, games_since_goal (cap 15 games) and
# days_since_game need each player's immediately-prior snapshot rows;
# club/opp rest days need prior Team-Stats rows; the lineup projector looks
# back 10 games. 60 days covers every in-season break with margin.
HISTORY_WINDOW_DAYS = int(os.environ.get("ML_HISTORY_WINDOW_DAYS", "60"))

ARTIFACT_STALE_DAYS = 7

# The 16 columns the migration must have published on the player snapshots
# (docs/supabase_shot_columns.md): the 14 shot/TOI aggregates + 2 club abbrevs.
REQUIRED_SNAPSHOT_COLUMNS = [
    "sd_shots_pg",
    "sd_goals_pg",
    "sd_toi_pg",
    "sd_sh_pct",
    "r5_shots_pg",
    "r5_goals_pg",
    "r10_shots_pg",
    "r10_goals_pg",
    "r10_toi_pg",
    "ewm_goals",
    "ewm_shots",
    "career_games",
    "career_goals_pg",
    "career_shots_pg",
    "team_abbrev",
    "opponent_abbrev",
]

PICK_DB_COLUMNS = ["date", "player_id", "name", "team_name", "stat", "tims", "gpg", "hgpg", "ppg"]
CHALLENGE_GROUPS = [1, 2, 3]
EXPECTED_PICKS = 3


# ---------------------------------------------------------------- artifact


def load_artifact(s3_client, bucket, prefix):
    """Lexically latest <prefix>/artifact_<through>_<seed>.joblib -> bundle.

    Names sort by train-through date, so 'latest' picks the newest vintage.

    KEY KATA (dated-prefix-latest, not a pinned key): the owner's day-of
    refresh uploads `artifact_<train_through>_<seed>.joblib` under ARTIFACT
    PREFIX (ML_ARTIFACT_PREFIX, default predict/ml); every old vintage stays
    in place. The Lambda lists the prefix and takes the lexically greatest
    .joblib - newest train_through wins, no config file to keep in sync,
    no redeploy on a refresh, and a rollback is a manual
    `aws s3 cp` of the older key back under the prefix (or trim the newer
    key away) - the newest artifact is always "whatever the last
    `train_export.py --upload` pushed", by construction. (An explicit
    event override `{"artifact_key": "..."}` is not supported; the prefix
    scan stays the single source of truth on purpose.)
    """
    listing = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix.rstrip("/") + "/")
    keys = sorted(k for k in (o["Key"] for o in listing.get("Contents", [])) if k.endswith(".joblib"))
    if not keys:
        raise RuntimeError(f"no artifact .joblib found under s3://{bucket}/{prefix}")
    key = keys[-1]
    payload = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    import joblib  # noqa: PLC0415  (S3 payload-time, artifact bytes already in memory) (S3 payload-time import keeps container cold-start flat)

    bundle = joblib.load(io.BytesIO(payload))
    return key, normalise_bundle(bundle, key=key)


def normalise_bundle(bundle, key="artifact"):
    """Map either accepted artifact design onto ONE canonical shape.

    Canonical (the legacy train_export.py design, kept as the envelope):
        hgb/lgbm/logreg/ranker  fitted estimators (ranker may be absent)
        all_features            the artifact's ordered feature list
        categorical_features    the artifact's categorical feature names
        numeric_features        logreg's numeric-only feature list
        categories              {col: [values]} trained vocabulary
        metadata                train_through / seed / git_hash / generated_at
        derived_params          the documented ensemble scoring recipe (opt.)

    Design A - ARTIFACT CONTRACT (sibling train_export.py): a zlib joblib
    dict with top-level version:int(1), train_through, seed, git_hash,
    generated_at, feature_order:[ordered features], categorical_maps:dict,
    derived_params (the documented ensemble recipe), models: dict of the
    4 pickled estimators - plus ONLY those keys (metadata is synthesized
    from the top-level fields).

    Missing required keys raise with the exact key names either way.
    """
    contract = {"models", "feature_order"}.issubset(bundle)
    source = "contract" if contract else "legacy"

    def _pick(candidates, required):
        for candidate in candidates:
            value = bundle.get(candidate) if isinstance(bundle, dict) else None
            if value is not None:
                return value
        if required:
            raise RuntimeError(
                f"artifact {key} ({source} design) is malformed: required key(s) "
                f"absent - looked for exactly {candidates}"
            )
        return None

    models = bundle.get("models") if isinstance(bundle.get("models"), dict) else {}
    names = ("hgb", "lgbm", "logreg")
    members = {n: (models.get(n) if contract else _pick([n], required=False)) for n in names}
    absent = [n for n, m in members.items() if m is None]
    if absent:
        raise RuntimeError(
            f"artifact {key} ({source} design) is malformed: missing ensemble "
            f"member(s) {absent} - missing required artifact key(s)"
        )

    feature_order = _pick(["feature_order", "all_features"], required=True)
    categories = _pick(["categorical_maps", "categories"], required=False) or {}
    categorical_features = (
        _pick(["categorical_features"], required=False)
        or (sorted(categories) if categories else None)
        or _pick(["categorical_features"], required=True)
    )
    numeric_features = _pick(["numeric_features"], required=False) or [
        col for col in feature_order if col not in set(categorical_features)
    ]

    if contract:
        legacy_meta = bundle.get("metadata") if isinstance(bundle.get("metadata"), dict) else {}
        meta = {
            **legacy_meta,
            **{
                k: bundle[k]
                for k in ("train_through", "seed", "git_hash", "generated_at", "version")
                if bundle.get(k) is not None
            },
        }
    else:
        meta = _pick(["metadata"], required=True)
        if not isinstance(meta, dict):
            raise RuntimeError(f"artifact {key}: metadata must be a dict")
    return {
        "hgb": members["hgb"],
        "lgbm": members["lgbm"],
        "logreg": members["logreg"],
        "ranker": models.get("ranker") if contract else _pick(["ranker"], required=False),
        "all_features": list(feature_order),
        "categorical_features": list(categorical_features),
        "numeric_features": list(numeric_features),
        "categories": categories,
        "metadata": meta,
        "derived_params": bundle.get("derived_params"),
        "required_snapshot_columns": bundle.get("required_snapshot_columns"),
    }


def staleness_note(bundle):
    meta = bundle["metadata"]
    through = str(meta.get("train_through", ""))
    try:
        age_days = (datetime.date.today() - datetime.date.fromisoformat(through)).days
    except ValueError:
        return None
    if age_days > ARTIFACT_STALE_DAYS:
        print(
            f"WARNING: artifact trains through {through}, which is {age_days} days stale "
            f"(refresh with a local train_export.py --upload run)",
            flush=True,
        )
    return age_days


# ------------------------------------------------------------- Supabase read


def fetch_window(table, order_cols, lo, hi, chunk=10_000):
    """date-bounded, offset-paged read of one backtrack table (read-only)."""
    query = SUPABASE_ADMIN_CLIENT.table(table).select("*").gte("date", lo).lt("date", hi)
    for col in order_cols:
        query = query.order(col)
    frames, offset = [], 0
    while True:
        rows = query.range(offset, offset + chunk - 1).execute().data
        if not rows:
            break
        frames.append(pd.DataFrame(rows))
        offset += len(rows)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_prediction_tables(env, on_date):
    history_lo = (datetime.date.fromisoformat(on_date) - datetime.timedelta(days=HISTORY_WINDOW_DAYS)).isoformat()
    hi = (datetime.date.fromisoformat(on_date) + datetime.timedelta(days=1)).isoformat()
    players = fetch_window(model.player_table(env), ["date", "player_id"], history_lo, hi)
    if players.empty or (players["date"].astype(str) == on_date).sum() == 0:
        raise RuntimeError(
            f"no snapshot rows for {on_date} in {model.player_table(env)}; the snapshot build did not run"
        )
    teams = fetch_window(model.team_stats_table(env), ["date", "team_abbrev"], history_lo, hi)
    goalies = fetch_window(model.goalie_table(env), ["date", "player_id"], history_lo, hi)
    return players, teams, goalies


def require_snapshot_columns(players_today):
    missing = [col for col in REQUIRED_SNAPSHOT_COLUMNS if col not in players_today.columns]
    if missing:
        raise RuntimeError(
            f"{model.player_table(ENV)} today's rows lack the 16 migrated columns "
            f"(docs/supabase_shot_columns.md): missing exactly {missing}. "
            "Run/finish the shot-columns backfill for this env before the ML pick pass."
        )


# ----------------------------------------------------------------- scoring


def encode_frame(bundle, frame):
    """Rebuild the training encoding for prediction rows: same column ORDER
    and the artifact's category vocabulary - exactly what model.categorise
    produced on the training fold."""
    today = frame.copy()
    x = pd.DataFrame(index=today.index)
    for col in bundle["all_features"]:
        values = today[col]
        if col in bundle["categorical_features"]:
            values = values.astype(str)
            vocab = set(map(str, bundle["categories"].get(col, [])))
            x[col] = values.where(values.isin(vocab)).astype("category")
        else:
            x[col] = pd.to_numeric(values, errors="coerce")
    return x


def ensemble_scores(bundle, frame):
    """Per-model scores + the ensemble mean of within-group rank percentiles.

    Exactly model.run_folds' rule: predict each member, fillna(0), rank
    percentile within (date, tims), average hgb+lgbm+logreg. The ranker is
    exported for traceability but excluded (the measured result is the
    3-member ensemble).
    """
    x = encode_frame(bundle, frame)
    sim = frame.copy()
    for name, member, cols in (
        ("hgb", bundle["hgb"], bundle["all_features"]),
        ("lgbm", bundle["lgbm"], bundle["all_features"]),
        ("logreg", bundle["logreg"], bundle["numeric_features"]),
    ):
        sim[name] = np.asarray(member.predict_proba(x[cols])[:, 1], dtype=float)
    sim[["hgb", "lgbm", "logreg"]] = sim[["hgb", "lgbm", "logreg"]].fillna(0.0)
    ranks = sim.groupby(["date", "tims"])[["hgb", "lgbm", "logreg"]].rank(pct=True)
    sim["ensemble"] = ranks.mean(axis=1)
    return sim


# ------------------------------------------------------------------ picks


def validate_picks(picks, on_date):
    """The daily sim's shape: exactly one pick per tims group 1/2/3."""
    groups = picks["tims"].unique().tolist()
    if sorted(groups) != CHALLENGE_GROUPS or len(picks) != EXPECTED_PICKS:
        raise RuntimeError(
            f"expected exactly {EXPECTED_PICKS} picks (one per tims group "
            f"{CHALLENGE_GROUPS}) for {on_date}, got {len(picks)} across "
            f"tims {sorted(groups)}"
        )


def pick_rows_to_picks_db(picks):
    """Whitelist into the Picks-{ENV} write shape (PICK_DB_COLUMNS)."""
    rows = []
    for _, row in picks.iterrows():
        stat = row.get("ensemble", np.nan)
        if isinstance(stat, float) and not np.isfinite(stat):
            stat = None
        else:
            stat = round(float(stat) * 100, 1)
        values = []
        for key in PICK_DB_COLUMNS:
            if key == "stat":
                values.append(stat)
            elif key in ("player_id", "tims"):
                values.append(int(row[key]))
            else:
                value = row.get(key)
                value = None if value is None or (isinstance(value, float) and np.isnan(value)) else value
                if isinstance(value, np.generic):
                    value = value.item()
                values.append(value)
        rows.append(dict(zip(PICK_DB_COLUMNS, values)))
    return rows


# ------------------------------------------------------------------ handler


def today_et():
    import pytz  # noqa: PLC0415  (lazy; single call per invoke)

    return datetime.datetime.now(pytz.timezone("America/Toronto")).strftime("%Y-%m-%d")


def handler(event, context):
    event = event or {}
    on_date = str(event.get("date") or today_et())
    dry = bool(event.get("dry"))

    bucket = os.environ.get("ML_S3_BUCKET")
    prefix = os.environ.get("ML_ARTIFACT_PREFIX", "predict/ml")
    if not bucket:
        raise RuntimeError("ML_S3_BUCKET is not configured on this Lambda")

    key, bundle = load_artifact(boto3.client("s3"), bucket, prefix)
    meta = bundle["metadata"]
    stale = staleness_note(bundle)
    print(
        f"artifact {key}: through={meta.get('train_through')} seed={meta.get('seed')} "
        f"git={meta.get('git_hash')} age={stale}d",
        flush=True,
    )

    players, teams, goalies = load_prediction_tables(ENV, on_date)
    require_snapshot_columns(players[players["date"].astype(str) == on_date])
    frame = model.build_frame(players, teams, goalies, source="supabase")
    frame = model.add_derived(frame, teams, source="supabase")
    frame = frame[frame["date"].astype(str) == on_date].reset_index(drop=True)
    if not set(bundle["all_features"]).issubset(frame.columns):
        missing = [c for c in bundle["all_features"] if c not in frame.columns]
        raise RuntimeError(f"frame build lacks artifact features {missing}; shape/order mismatch")
    if frame.empty:
        raise RuntimeError(f"frame produced no rows for {on_date}")

    sim = ensemble_scores(bundle, frame)
    picks = model.daily_picks(sim, "ensemble")
    validate_picks(picks, on_date)
    rows = pick_rows_to_picks_db(picks)
    if not dry:
        save_to_db(rows)
    mean_max_prob = round(float(pd.to_numeric(picks["ensemble"], errors="coerce").mean()), 4) if len(picks) else None
    print(
        json.dumps(
            {
                "log": "SMARTSCORE_ML_PICKS",
                "date": on_date,
                "n_picks": len(rows),
                "mean_max_prob": mean_max_prob,
                "ensemble_scale": "within-(date,tims) rank percentile",
                "artifact": {
                    "key": key,
                    "train_through": meta.get("train_through"),
                    "seed": meta.get("seed"),
                },
                "published": not dry,
            },
            default=str,
        ),
        flush=True,
    )
    if dry:
        print(f"dry: assembled {len(rows)} ML picks for {on_date} (no Picks write)", flush=True)

    return {
        "statusCode": 200,
        "date": on_date,
        "artifact": {"bucket": bucket, "key": key, "through": meta.get("train_through"), "seed": meta.get("seed")},
        "picks": rows,
        "dry": dry,
    }
