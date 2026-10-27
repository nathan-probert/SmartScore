"""Predict-only ML picks, on a scheduled GitHub Actions run: SmartScore
ensemble -> Picks-{ENV}. Replaces the retired MLPicks Lambda/image surface
(docs/ml-picks-runbook.md) - SAME inference path, $0.00 infra.

Owner rules this script implements (docs/ml-picks-runbook.md):

  * training NEVER happens here (or anywhere in CI) - it only loads a
    versioned joblib artifact that the owner's train_export.py --upload run
    pushed from the local machine;
  * it reads TODAY's player rows from the LIVE snapshot table
    (Player-Snapshots-{ENV}, the daily build) and HISTORY rows strictly
    before the slate date from the backtrack tables (Player-Snapshots- /
    Team-Stats- / Goalie-Snapshots-backtrack-), joined through
    ``model.build_frame(..., source="supabase")`` + ``model.add_derived`` -
    model.py's math is imported, never forked; requires the 16 shot/TOI +
    club columns (docs/supabase_shot_columns.md) on today's (attached)
    frame with a hard fail naming exactly what is missing, and asserts the
    built frame has the artifact's exact ordered feature columns;

  STAGE 1 HONEST APPROXIMATION: rolling/career values taken from the last
  published prior day; exact same-day recomputation is stage 2 (a per-game
  raw-columns publish) and is planned.

  * it picks one player per tims group (model.daily_picks' rule, applied by
    the ensemble's mean within-group rank percentile) and publishes through
    the existing Picks-{ENV} conventions, reached via the publish_public_db
    row shape, through a schema-safe wrapper (save_picks_db below) that
    probes the table's live columns BEFORE the delete and restores the
    pre-wipe rows if the insert fails - no manual Supabase writes.

Manual trigger: the workflow's ``date`` input (or ``--date YYYY-MM-DD``)
overrides ET-today; ``dry``/``--dry`` skips the Picks write (smoke run).

Artifact source (the artifact-fact convention), in this documented order:
(1) ``--artifact PATH`` (a local file, e.g. one committed under data/ml/) -
always wins; (2) S3 - the lexically latest .joblib under the dated prefix
(with credentials whatever the last ``train_export.py --upload`` pushed);
(3) Supabase Storage - ``ML_STORAGE_BUCKET`` (default bucket ``ml-artifacts``,
prefix ``predict/ml``, same key convention) - the no-AWS-credentials path
the ml-picks workflow runs by DEFAULT (same SUPABASE_* secrets inference
needs; load_artifact_storage below). If S3 is configured but its
credentials are absent, the run falls through to Storage. No rewrite, no
fork: every function below is the former smartscore/lambda/predict_picks/
handler.py logic verbatim, only moved and re-anchored.
"""

import argparse
import datetime
import io
import json
import os
import sys
from pathlib import Path

import boto3
import numpy as np
import pandas as pd

# model.py ships in this same backtrack directory; config/utility live one
# tree up in smartscore/ - the SAME modules the handler imported, just
# reached through the repo layout instead of a container image.
_here = Path(__file__).resolve()
_root = _here.parents[2]
sys.path.insert(0, str(_root / "smartscore"))
sys.path.insert(1, str(_here.parent))
import model  # noqa: E402

from config import ENV, SUPABASE_ADMIN_CLIENT  # noqa: E402

# The existing Picks-{ENV} conventions (save_to_db, utility.py) are kept as
# documentation-of-record; the write itself goes through save_picks_db below
# (schema-probe -> wipe -> insert -> restore-on-failure) because save_to_db's
# unscoped POST wipes the table before the insert can fail.

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

# The 14 shot/TOI aggregates today's LIVE rows lack; attach_prior_features
# copies each from the player's last published prior backtrack row (stage 1
# approximation; see attach_prior_features + docs/ml-picks-runbook.md).
APPROX_ATTACH_COLUMNS = [col for col in REQUIRED_SNAPSHOT_COLUMNS if col not in ("team_abbrev", "opponent_abbrev")]

PICK_DB_COLUMNS = ["date", "player_id", "name", "team_name", "stat", "tims", "gpg", "hgpg", "ppg"]
CHALLENGE_GROUPS = [1, 2, 3]
EXPECTED_PICKS = 3

# Whitelist keys that NO Picks-{ENV} table has (dev spot-checked 2026-10-27:
# PostgREST 42703 "column Picks-{ENV}.ppg does not exist" on both envs). The
# legacy write helper WIPES the table before it inserts (utility.py:409), so
# one unsupported key in the payload = wipe succeeds, insert fails PGRST204,
# table left empty. The whitelist keeps the training-frame keys for the
# record; REMOVE_KEYS decides what is actually serialized.
REMOVE_KEYS = ["ppg"]


# ---------------------------------------------------------------- artifact


def load_artifact(s3_client, bucket, prefix):
    """Lexically latest <prefix>/artifact_<through>_<seed>.joblib -> bundle.

    Names sort by train-through date, so 'latest' picks the newest vintage.

    KEY KATA (dated-prefix-latest, not a pinned key): the owner's day-of
    refresh uploads `artifact_<train_through>_<seed>.joblib` under ARTIFACT
    PREFIX (ML_ARTIFACT_PREFIX, default predict/ml); every old vintage stays
    in place. The daily job lists the prefix and takes the lexically greatest
    .joblib - newest train_through wins, no config file to keep in sync,
    no redeploy on a refresh, and a rollback is a manual
    `aws s3 cp` of the older key back under the prefix (or trim the newer
    key away) - the newest artifact is always "whatever the last
    `train_export.py --upload` pushed", by construction. (An explicit input
    override `{"artifact_key": "..."}` is not supported; the prefix
    scan stays the single source of truth on purpose.)
    """
    listing = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix.rstrip("/") + "/")
    keys = sorted(k for k in (o["Key"] for o in listing.get("Contents", [])) if k.endswith(".joblib"))
    if not keys:
        raise RuntimeError(f"no artifact .joblib found under s3://{bucket}/{prefix}")
    key = keys[-1]
    payload = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    import joblib  # noqa: PLC0415  (payload-time import keeps the runner boot flat)

    bundle = joblib.load(io.BytesIO(payload))
    return key, normalise_bundle(bundle, key=key)


def load_artifact_file(path):
    """Fallback source: read a LOCAL .joblib (same bundle -> same canonical
    shape). Used when no AWS credentials are configured - the workflow then
    fetches the artifact itself (or one is committed under data/ml/), and
    this keeps the DOWNSTREAM inference identical either way."""
    import joblib  # noqa: PLC0415

    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"artifact file not found: {path}")
    bundle = joblib.load(path)
    return str(path), normalise_bundle(bundle, key=path.name)


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
    dict with top-level version:int, train_through, seed, git_hash,
    generated_at, feature_order:[ordered features], categorical_maps:dict,
    derived_params (the documented ensemble recipe), models: dict of the
    pickled estimators - plus ONLY those keys (metadata is synthesized
    from the top-level fields).

    Version 2 artifacts carry ``logreg`` as the FULL fitted pipeline
    (SimpleImputer(median) -> StandardScaler -> LogisticRegression), the exact
    object chain fit_logreg produced, so the member runs NaN-bearing live
    frames natively. Version 1 artifacts (pre-fix) exported the bare
    inner LogisticRegression - broken on NaN-bearing numeric frames, since the
    bare LR never saw the imputer/scaler its training pipeline ran - and are
    auto-patched here with a LOUD "LEGACY ARTIFACT FORMAT" warning: the
    adapter imputes medians and rescales PER RUN rather than using the
    train-fold preprocessing (not carried in v1), so retrain with the fixed
    train_export at your next owner-run to get v2.

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

    # ARTIFACT VERSION 2 primary path: logreg is the full fitted preprocessing
    # pipeline (SimpleImputer -> StandardScaler -> LogisticRegression), so the
    # NaN-bearing live frames impute/rescale inside the member exactly as they
    # did at training. Version 1 (and ANY bare-LR member, whether contract or
    # legacy design) falls back to a per-run adapter with a LOUD retrain
    # warning. Detection tolerates the Fake-style stub members used by tests
    # (no ``steps`` is only sufficient with sklearn's ``coef_`` present).
    logreg = members["logreg"]
    if not hasattr(logreg, "steps") and hasattr(logreg, "coef_"):
        import pandas as pd  # noqa: PLC0415  (hot path import kept lean)

        print(
            "LEGACY ARTIFACT FORMAT - retrain with the fixed train_export "
            f"(artifact {key} ships v{meta.get('version', 1)} which exported the bare "
            "logistic estimator without its train-time preprocessing pipeline; "
            "falling back to a per-run median-impute/scale adapter)",
            flush=True,
        )

        class _LegacyLogRegAdapter:
            """Per-run preprocessing patched around a bare v1 logreg member.

            The v1 artifact lost SimpleImputer/StandardScaler at export, so a
            v1 artifact fed NaN-bearing numeric frames crashed (ValueError:
            Input X contains NaN). Patch: impute medians + standardise per
            predict call, then run the bare LR. NOT the training-fold medians
            (v2 fixes that); a ranking-quality degradation to be retired by the
            next train_export --upload.
            """

            def __init__(self, estimator):
                self._estimator = estimator

            def predict_proba(self, x):
                from sklearn.impute import SimpleImputer  # noqa: PLC0415
                from sklearn.preprocessing import StandardScaler  # noqa: PLC0415

                arr = pd.DataFrame(x).to_numpy(dtype=float)
                # keep_empty_features: a live window can carry an all-NaN
                # column (rank_* on a tims=0-heavy slate, opp-goalie gaps);
                # without the flag SimpleImputer DROPS it and the bare LR's
                # 40-feature expectation mis-shapes. All-NaN columns impute
                # with the training-era filler (0) instead.
                arr = SimpleImputer(strategy="median", keep_empty_features=True).fit_transform(arr)
                arr = StandardScaler().fit_transform(arr)
                return self._estimator.predict_proba(arr)

        logreg = _LegacyLogRegAdapter(logreg)

    return {
        "hgb": members["hgb"],
        "lgbm": members["lgbm"],
        "logreg": logreg,
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


def live_player_rows(env, on_date):
    """Today's player rows from the LIVE snapshot table (Player-Snapshots-{env}).

    The daily snapshot build (the daily publish job) writes each slate's
    pre-game rows to the live table; the backtrack tables only gain a date's
    rows AFTER that day's games end and the daily publish runs. So TODAY's
    prediction rows live HERE, and an empty live table means "the daily
    snapshot build has not run for today yet" - a NORMAL state, not an error:
    a WARNING is printed and ``None`` returned so the caller can skip the run
    cleanly (no picks written, exit 0, no traceback). Read-only, offset-paged.
    """
    table = f"Player-Snapshots-{env}"
    frames, offset = [], 0
    while True:
        rows = (
            SUPABASE_ADMIN_CLIENT.table(table)
            .select("*")
            .eq("date", on_date)
            .range(offset, offset + 10_000 - 1)
            .execute()
            .data
        )
        if not rows:
            break
        frames.append(pd.DataFrame(rows))
        offset += len(rows)
    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if frame.empty:
        print(
            f"WARNING: no live snapshot rows for {on_date} - the daily snapshot build has not "
            f"run for today yet; no picks computed",
            flush=True,
        )
        return None
    return frame


def history_rows(env, before_date):
    """Published history rows STRICTLY date < before_date from the three
    backtrack tables ({Player,Team,Goalie}-*-backtrack-{env}) via the
    existing month-window pagination (model._month_windows, the same windows
    fetch_table uses). Read-only.

    Published-lag semantics, in order of loudness:

      * a game-date inside the window (a Team-Stats date) that the player
        table has NO rows for -> ONE warn listing the missing dates, then
        CONTINUE (a single day can be absent from the player grain without
        the archive being unusable);
      * the last published player row is NOT yesterday -> 'backtrack publish
        appears stale' WARN (the daily publish normally lands the previous
        day's rows the same night);
      * the last published player row is >7 days old -> the career-staleness
        LOUD watch line (career_* columns attached below would be market
        stale); still proceed.
    """
    lo = (datetime.date.fromisoformat(before_date) - datetime.timedelta(days=HISTORY_WINDOW_DAYS)).isoformat()
    hi = before_date
    players_parts, teams_parts, goalie_parts = [], [], []
    for win_lo, win_hi in model._month_windows(
        lo, (datetime.date.fromisoformat(before_date) - datetime.timedelta(days=1)).isoformat()
    ):
        players_parts.append(fetch_window(model.player_table(env), ["date", "player_id"], win_lo, min(win_hi, hi)))
        teams_parts.append(fetch_window(model.team_stats_table(env), ["date", "team_abbrev"], win_lo, min(win_hi, hi)))
        goalie_parts.append(fetch_window(model.goalie_table(env), ["date", "player_id"], win_lo, min(win_hi, hi)))
    players = _concat_nonempty(players_parts)
    teams = _concat_nonempty(teams_parts)
    goalies = _concat_nonempty(goalie_parts)
    if players.empty:
        print(
            f"WARNING: backtrack publish appears stale: last published none (no rows in "
            f"{model.player_table(env)} within the {HISTORY_WINDOW_DAYS}-day window before {before_date})",
            flush=True,
        )
        return players, teams, goalies
    snapped_dates = set(players["date"].astype(str))
    game_dates = set(teams["date"].astype(str)) if not teams.empty else set()
    missing = sorted(game_dates - snapped_dates)
    if missing:
        print(
            f"WARN: {model.player_table(env)} has no snapshot rows for game date(s) {missing} inside the "
            f"{HISTORY_WINDOW_DAYS}-day window - continuing with the days that ARE published",
            flush=True,
        )
    last = max(snapped_dates)
    lag_days = (datetime.date.fromisoformat(before_date) - datetime.date.fromisoformat(last)).days
    if lag_days > 1:
        print(f"WARNING: backtrack publish appears stale: last published {last}", flush=True)
    if lag_days > ARTIFACT_STALE_DAYS:
        print(
            f"STALENESS WATCH: {model.player_table(env)} last published {last} ({lag_days} days before "
            f"{before_date}) - career_* features attached from that day are >7 days stale; publish the "
            f"backtrack archive before trusting the picks",
            flush=True,
        )
    return players, teams, goalies


def _concat_nonempty(frames):
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def attach_prior_features(live_players, history_players, before_date):
    """STAGE 1 HONEST APPROXIMATION (documented verbatim in the runbook):

    stage 1: honest approximation: rolling/career values taken from the last
    published prior day; exact same-day recomputation is stage 2 (a per-game
    raw-columns publish) and is planned.

    Today's live rows (Player-Snapshots-{env}) carry the slate/odds/lineup
    columns but NOT the 14 shot/TOI aggregates (sd_*, r5_*, r10_*, ewm_*,
    career_*) - those are published on the backtrack grain only (smoke
    2026-10-27: live columns stop at odds/lineup/opp-goalie; the 16 migrated
    columns live on Player-Snapshots-backtrack-prod). For each player, attach
    the values of the missing aggregates (+ the 2 club abbrevs when absent -
    provenance, not rolling math) from that player's LAST published prior row
    (max(date) < before_date). At most 1 day stale for rolling columns;
    puts career_* right within the published window.

    A player with NO prior published row (call-up / first artifact coverage)
    gets NaN on the attached columns and a LOUD warn; the NaN stays and the
    artifact's already-committed keep-empty-features imputer resolves the row.
    """
    live = live_players.copy()
    hist = history_players.copy()
    if not hist.empty:
        hist["date"] = hist["date"].astype(str)
        hist = hist[hist["date"] < before_date].sort_values("date")
        prior = hist.drop_duplicates("player_id", keep="last").set_index("player_id")
    else:
        prior = pd.DataFrame()
    attach_cols = [col for col in APPROX_ATTACH_COLUMNS + ["team_abbrev", "opponent_abbrev"] if col not in live.columns]
    for col in attach_cols:
        if col in prior.columns:
            live[col] = live["player_id"].map(prior[col])
    uncovered_ids = set(live.loc[~live["player_id"].isin(prior.index), "player_id"])
    if uncovered_ids:
        names = live[live["player_id"].isin(uncovered_ids)]["name"].unique().tolist()
        print(
            f"WARNING: {len(uncovered_ids)} today-row player(s) have NO prior published row in "
            f"{model.player_table(ENV)} (call-up / first artifact coverage): "
            f"players {names} (ids {sorted(uncovered_ids)}) - their attached rolling/career features "
            "stay NaN; the artifact's keep-empty-features imputer resolves the row",
            flush=True,
        )
    return live


def require_snapshot_columns(players_today):
    missing = [col for col in REQUIRED_SNAPSHOT_COLUMNS if col not in players_today.columns]
    if missing:
        raise RuntimeError(
            f"Player-Snapshots-{ENV}: today's (attached) rows lack the 16 migrated columns "
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
    """Whitelist into the Picks-{ENV} write shape (PICK_DB_COLUMNS).

    REMOVE_KEYS are dropped here, before anything reaches the write helper:
    the serialization is exactly the Picks table's live columns (see
    pick_daily_bundle's sibling note in the module docstring - a table schema
    change updates the whitelist AND a migration, or REMOVE_KEYS).
    """
    write_keys = [key for key in PICK_DB_COLUMNS if key not in REMOVE_KEYS]
    rows = []
    for _, row in picks.iterrows():
        stat = row.get("ensemble", np.nan)
        if isinstance(stat, float) and not np.isfinite(stat):
            stat = None
        else:
            stat = round(float(stat) * 100, 1)
        values = []
        for key in write_keys:
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
        rows.append(dict(zip(write_keys, values)))
    return rows


# ------------------------------------------------------------ schema-safe write


def _unsupported_picks_keys(table, keys):
    """Payload keys that are NOT columns of ``table`` (a fail-safe pre-delete check).

    The codebase's cheapest stable schema probe (the same select-a-column
    pattern ``publish_shot_cols.columns_landed`` uses at
    publish_shot_cols.py:292): PostgREST 400s ANY request naming an unknown
    column, so one select naming ALL the payload keys can only succeed when
    every key is a real column - and, unlike reading a first row, the probe
    also proves the columns exist on an EMPTY table (the exact state the
    smoke-run wipe left Picks-dev in). Falls back to per-key probes so the
    caller's raise names exactly which keys the table rejects; any probe
    error means "unreachable or unknown column", and an unreachable table is
    the safe direction (the write aborts before ANY delete).
    """
    keys = sorted(keys)
    try:
        SUPABASE_ADMIN_CLIENT.table(table).select(",".join(keys)).limit(1).execute()
    except Exception:  # noqa: BLE001 - probe failure is only the fast path; classify per key
        unsupported = []
        for key in keys:
            try:
                SUPABASE_ADMIN_CLIENT.table(table).select(key).limit(1).execute()
            except Exception:  # noqa: BLE001 - any failure means the key is not a real column
                unsupported.append(key)
        return unsupported
    return []


def _picks_table_rows(table):
    """Every row of ``table`` (the restore material for save_picks_db)."""
    return SUPABASE_ADMIN_CLIENT.table(table).select("*").execute().data or []


def save_picks_db(rows, date):
    """Replace the ENTIRE Picks-{ENV} table with the run's picks - but only
    when the insert is safe.

    The legacy write helper's unscoped POST (save_to_db -> utility.py:409/411,
    reached by run_daily_picks until this wrapper) wipes the table BEFORE it
    inserts, so an insert failure leaves the table EMPTY. The smoke run did
    exactly that to Picks-dev: the payload shipped a `ppg` key, the wipe
    succeeded, the insert failed PGRST204. This wrapper keeps the existing
    wipe-then-insert order (the published single-day convention:
    delete-all then reinsert, ids restarted at 1 like save_to_db) but makes
    it fail-safe:

      1. BEFORE ANY delete: schema-probe every payload key against the live
         Picks-{ENV} table (_unsupported_picks_keys). One unsupported key
         raises and the table is untouched - the old state survives, and the
         failure names the offending keys.
      2. The full pre-wipe rows are read into memory (``prior``), the same
         capture-before-wipe move _prior_goalie_values makes for its own
         wipe (utility.py:74-77).
      3. Wipe (the same ``delete().neq("id", 0)`` the legacy helper uses),
         then insert the new rows with ``id = 1 + i`` (save_to_db's id
         convention, utility.py:117).
      4. If the insert (or the post-write count verify) fails: a best-effort
         re-insert of ``prior`` (its keys are the table's own columns by
         construction) and RAISE - so a failed run leaves the realized end
         state as the OLD table, never 0.

    NOT a Postgres transaction: if the process dies between the delete and
    the insert the table stays empty until the next successful run, and if
    the restore insert itself fails BOTH errors are reported in one raise
    (the wipe is then the realized end state, stated loudly). A failed
    DELETE needs no restore: PostgREST's delete is atomic, so its exception
    leaves the old rows in place.
    """
    if not rows:
        print(
            f"save_picks_db: no pick rows for {date}; NO write (wipe skipped - the Picks table is untouched)",
            flush=True,
        )
        return
    table = f"Picks-{ENV}"
    unsupported = _unsupported_picks_keys(table, {key for row in rows for key in row})
    if unsupported:
        raise RuntimeError(
            f"picks write aborted BEFORE any delete: {table} has no column(s) {unsupported} "
            "(payload keys must be a subset of the live table columns - fix REMOVE_KEYS "
            "or add the columns; the legacy wipe-then-insert would have emptied the table)"
        )
    prior = _picks_table_rows(table)
    try:
        # Wipe, then insert: identical semantics to the legacy unscoped POST
        # (utility.py:409/411), keeping the ids-restart-at-1 convention.
        SUPABASE_ADMIN_CLIENT.table(table).delete().neq("id", 0).execute()
        payload = [dict(row, id=i + 1) for i, row in enumerate(rows)]
        SUPABASE_ADMIN_CLIENT.table(table).upsert(payload).execute()
        persisted = SUPABASE_ADMIN_CLIENT.table(table).select("id").execute().data or []
        if len(persisted) != len(payload):
            raise RuntimeError(f"post-write verify mismatch: {len(persisted)} rows published, expected {len(payload)}")
    except Exception as insert_error:  # noqa: BLE001 - restored below, then re-raised chained
        try:
            if prior:
                SUPABASE_ADMIN_CLIENT.table(table).upsert(prior).execute()
            report = (
                f"picks insert FAILED; the pre-wipe rows were re-inserted, so {table} "
                f"holds its OLD state. original error: {insert_error!r}"
            )
        except Exception as restore_error:  # noqa: BLE001 - the restore report must still name both failures
            report = (
                f"picks insert FAILED and the best-effort RESTORE ALSO failed - {table} "
                f"stays WIPE-EMPTY. original error: {insert_error!r}; restore error: {restore_error!r}"
            )
        print(f"save_picks_db: {report}", flush=True)
        raise RuntimeError(report) from insert_error


# ------------------------------------------------------------------ run


def today_et():
    import pytz  # noqa: PLC0415  (lazy; single call per run)

    return datetime.datetime.now(pytz.timezone("America/Toronto")).strftime("%Y-%m-%d")


def job_summary(on_date, key, meta, n_picks, mean_max_prob, dry):
    """Append the run's headline to $GITHUB_STEP_SUMMARY when one exists
    (on GitHub Actions); a no-op anywhere else."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        f"## ML picks run: {on_date}",
        "",
        f"- artifact: `{key}` (train_through {meta.get('train_through')}, seed {meta.get('seed')})",
        f"- picks published: {n_picks}{'' if not dry else ' (DRY - no Picks write)'}",
        f"- mean ensemble percentile: {mean_max_prob}",
    ]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _skip_run(on_date, key, meta, dry):
    """Clean soft skip: no live snapshot rows for the date -> the job summary
    line, a flat print, and a 200 without ANY exception (exit 0, no pickle/
    traceback on Actions). ``picks`` is empty and ``status`` is 'skipped', a
    marker a workflow can gate on for an explicit red if it ever needs one
    (the default presentation is a green no-op step)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [
        f"## ML picks run: {on_date} - SKIPPED",
        "",
        f"- no picks computed: no live snapshot rows for {on_date}",
        f"- artifact: `{key}` (train_through {meta.get('train_through')}, seed {meta.get('seed')})",
    ]
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    print(
        json.dumps(
            {
                "log": "SMARTSCORE_ML_PICKS",
                "date": on_date,
                "status": "skipped",
                "n_picks": 0,
                "published": False,
                "dry": dry,
            },
            default=str,
        ),
        flush=True,
    )
    return {
        "statusCode": 200,
        "date": on_date,
        "status": "skipped",
        "message": f"no picks computed: no live snapshot rows for {on_date}",
        "artifact": {
            "bucket": "(storage)",
            "key": key,
            "through": meta.get("train_through"),
            "seed": meta.get("seed"),
        },
        "picks": [],
        "dry": dry,
    }


def load_artifact_storage(client, bucket, prefix):
    """Lexically latest <prefix>/artifact_<through>_<seed>.joblib from
    Supabase Storage -> bundle (the SAME dated-prefix-latest convention as
    ``load_artifact``, no AWS anywhere).

    ``client`` is the ``supabase`` Client (config's SUPABASE_ADMIN_CLIENT -
    the service-role key the Actions runner already holds, so the artifact
    can sit in a PRIVATE bucket). The list/download match the S3 loader
    byte-for-byte in behaviour: list the prefix, sort by key, take the
    lexically greatest .joblib, download, joblib.load -> normalise_bundle.
    """
    prefix = prefix.rstrip("/")
    listing = client.storage.from_(bucket).list(prefix or "")
    keys = sorted(
        (entry["name"] for entry in listing if str(entry.get("name", "")).endswith(".joblib")),
        key=lambda name: f"{prefix}/{name}",
    )
    if not keys:
        raise RuntimeError(f"no artifact .joblib found under storage bucket {bucket}/{prefix}")
    key = f"{prefix}/{keys[-1]}" if prefix else keys[-1]
    payload = client.storage.from_(bucket).download(key)
    import joblib  # noqa: PLC0415  (payload-time import keeps the runner boot flat)

    bundle = joblib.load(io.BytesIO(payload))
    return key, normalise_bundle(bundle, key=key)


def pick_daily_bundle(bucket, prefix, artifact_path, storage_bucket=None, storage_prefix=None):
    """Resolve the artifact, in strict documented order:

    1. ``--artifact PATH`` (local file) - always wins;
    2. S3 ``bucket/prefix`` with credentials (the historical default);
    3. Supabase Storage ``storage_bucket/storage_prefix`` - the
       no-AWS-credentials path (same SUPABASE_* secrets inference needs).

    If an S3 bucket is configured but the credentials are absent/denied
    (NoCredentialsError / ClientError), the run FALLS THROUGH to Storage
    when one is configured, so a runner without AWS secrets still works.
    """
    if artifact_path:
        return load_artifact_file(artifact_path)
    if bucket:
        try:
            return load_artifact(boto3.client("s3"), bucket, prefix)
        except Exception as exc:  # noqa: BLE001  (boto3 raises a range: NoCredentialsError, ClientError, ...)
            if not storage_bucket:
                raise
            print(
                f"artifact: S3 read of s3://{bucket}/{prefix} unavailable "
                f"({type(exc).__name__}: {exc}); falling back to Supabase Storage",
                flush=True,
            )
    if storage_bucket:
        return load_artifact_storage(SUPABASE_ADMIN_CLIENT, storage_bucket, storage_prefix or prefix or "predict/ml")
    raise RuntimeError(
        "no artifact source: set ML_STORAGE_BUCKET (or pass --storage-bucket) for the "
        "default Supabase Storage read, set ML_S3_BUCKET (or pass --s3-bucket) for the "
        "S3 read, or pass --artifact PATH for the local fallback"
    )


def run(event=None, context=None):
    """The former Lambda handler, updated for the live/backtrack split:
    today's rows come from the LIVE table (skip with status='skipped', exit 0,
    when the daily build has not run yet), history from the backtrack tables;
    today's rolling/career features come from the last published prior row
    (stage 1 approximation - see attach_prior_features). `event` carries
    optional date / dry / artifact_path / s3_bucket / s3_prefix; `context`
    is kept in the signature so the lifted test surface stays drop-in."""
    event = event or {}
    on_date = str(event.get("date") or today_et())
    dry = bool(event.get("dry"))

    bucket = event.get("s3_bucket") or os.environ.get("ML_S3_BUCKET")
    prefix = event.get("s3_prefix") or os.environ.get("ML_ARTIFACT_PREFIX", "predict/ml")
    storage_bucket = event.get("storage_bucket") or os.environ.get("ML_STORAGE_BUCKET")
    storage_prefix = event.get("storage_prefix") or os.environ.get("ML_ARTIFACT_STORAGE_PREFIX", "predict/ml")

    key, bundle = pick_daily_bundle(bucket, prefix, event.get("artifact_path"), storage_bucket, storage_prefix)
    meta = bundle["metadata"]
    stale = staleness_note(bundle)
    print(
        f"artifact {key}: through={meta.get('train_through')} seed={meta.get('seed')} "
        f"git={meta.get('git_hash')} age={stale}d",
        flush=True,
    )

    live_players = live_player_rows(ENV, on_date)
    if live_players is None:
        # Clean soft skip: the daily snapshot build has not produced today's
        # rows yet (NORMAL on the early schedule) - no Picks write, exit 0.
        # The Actions job shows a green 'skipped' step, not a traceback - if a
        # workflow ever needs an explicit red, gate on result["status"].
        return _skip_run(on_date, key, meta, dry)
    history_players, teams, goalies = history_rows(ENV, on_date)
    players_today = attach_prior_features(live_players, history_players, on_date)
    require_snapshot_columns(players_today)
    players = (
        pd.concat([history_players, players_today], ignore_index=True) if not history_players.empty else players_today
    )
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
        # schema-safe full replace: the wipe commits only when the insert is
        # safe (probe before delete + restore-on-insert-failure); on failure
        # the run raises instead of leaving Picks-{ENV} empty.
        save_picks_db(rows, on_date)
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
    job_summary(on_date, key, meta, len(rows), mean_max_prob, dry)

    return {
        "statusCode": 200,
        "date": on_date,
        "status": "ok",
        "artifact": {
            "bucket": bucket or "(local file)",
            "key": key,
            "through": meta.get("train_through"),
            "seed": meta.get("seed"),
        },
        "picks": rows,
        "dry": dry,
    }


# Kept as the module's canonical entry name so callers (and tests) get the
# same interface the Lambda handler exposed; run() is the same function.
handler = run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=None, help="slate date YYYY-MM-DD (default: ET-today)")
    parser.add_argument("--dry", action="store_true", help="smoke run: assemble picks, skip the Picks write")
    parser.add_argument(
        "--artifact",
        default=os.environ.get("ML_ARTIFACT_PATH"),
        help="local .joblib path (fallback when AWS credentials or ML_S3_BUCKET are not configured)",
    )
    parser.add_argument("--s3-bucket", default=None, help="override the ML_S3_BUCKET env (default S3 source)")
    parser.add_argument("--s3-prefix", default=None, help="override the ML_ARTIFACT_PREFIX env (default predict/ml)")
    parser.add_argument(
        "--storage-bucket",
        default=None,
        help="Supabase Storage bucket override (default the ML_STORAGE_BUCKET env, e.g. ml-artifacts) - "
        "the no-AWS-credentials artifact source",
    )
    parser.add_argument(
        "--storage-prefix",
        default=None,
        help="Supabase Storage path override (default the ML_ARTIFACT_STORAGE_PREFIX env, predict/ml)",
    )
    args = parser.parse_args(argv)

    event = {}
    if args.date:
        event["date"] = args.date
    if args.dry:
        event["dry"] = True
    if args.artifact:
        event["artifact_path"] = args.artifact
    if args.s3_bucket:
        event["s3_bucket"] = args.s3_bucket
    if args.s3_prefix:
        event["s3_prefix"] = args.s3_prefix
    if args.storage_bucket:
        event["storage_bucket"] = args.storage_bucket
    if args.storage_prefix:
        event["storage_prefix"] = args.storage_prefix
    return run(event)


if __name__ == "__main__":
    main()
