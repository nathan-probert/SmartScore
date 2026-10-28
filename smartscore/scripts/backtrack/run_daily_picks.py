"""Predict-only ML picks, on a scheduled GitHub Actions run: SmartScore
ensemble -> Picks-{ENV}. Replaces the retired MLPicks Lambda/image surface
(docs/ml-picks-runbook.md) - SAME inference path, $0.00 infra.

Owner rules this script implements (docs/ml-picks-runbook.md):

  * training NEVER happens here (or anywhere in CI) - it only loads a
    versioned joblib artifact that the owner's train_export.py --upload run
    pushed from the local machine;
  * TODAY's prediction rows come from the LIVE snapshot table
    Player-Snapshots-{ENV} (what the pipeline's daily snapshot build
    publishes each morning); the BACKTRACK tables
    (Player-Snapshots-backtrack-{ENV}, Team-Stats-backtrack-{ENV},
    Goalie-Snapshots-backtrack-{ENV}) are used ONLY as history - they gain a
    date's rows after that day's games end and the publish runs, so a
    missing recent day is NORMAL: the run prints ONE warning and continues;
  * the 14 shot/TOI features for today's rows are recomputed EXACTLY by the
    SAME pandas core model.shot_features_from_games (imported, never forked)
    over the PUBLISHED per-game record in Supabase (the stage-2 raw columns
    game_goals/game_shots/game_toi_min on Player-Snapshots-backtrack-{ENV},
    added by 20261027_add_per_game_raw_columns_player_snapshots_backtrack.sql)
    plus one synthetic today-row per player; the rolling/career attach's
    stage-1 "last published row" approximation is GONE. The only inputs
    still not on the player table are the team/goalie rates, read at publish
    cadence from Team-Stats / Goalie-Snapshots-backtrack (a lagging one of
    those prints the runbook's staleness warning, never an exit);
    a day the backfill has not covered yet costs a WARNING, not the run;
  * when the LIVE snapshot itself has no rows for the pick date there is
    nothing to score: the run warns ("no picks computed: no live snapshot
    rows for <date>") and exits CLEANLY (status 0, no Picks write, no
    traceback) instead of hard-failing the schedule;
  * it requires the live snapshot's own context columns (team/opponent/
    goalie/rate fields - REQUIRED_LIVE_COLUMNS) with a hard fail naming
    exactly what is missing, and asserts the built frame has the artifact's
    exact ordered feature columns;
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
credentials are absent, the run falls through to Storage. The artifact
loading below is the former smartscore/lambda/predict_picks/handler.py
logic verbatim, only moved and re-anchored; the data-assembly functions
(today_live_rows / history_rows / derive_shot_block) implement the
owner-approved live-snapshot data flow documented in the bullets above.
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
from team_map import TEAM_ABBREV_TO_PLACE  # noqa: E402

from config import ENV, SUPABASE_ADMIN_CLIENT  # noqa: E402

# The existing Picks-{ENV} conventions (save_to_db, utility.py) are kept as
# documentation-of-record; the write itself goes through save_picks_db below
# (schema-probe -> wipe -> insert -> restore-on-failure) because save_to_db's
# unscoped POST wipes the table before the insert can fail.

# The HISTORY fetch is deliberately FULL (every published backtrack row
# strictly before the pick date): career_games / career_goals_pg /
# career_shots_pg are expanding CAREER sums and ewm_* is career-long - a
# 60-day window mis-states all of them for veterans. HISTORY_WINDOW_DAYS
# survives as the LOOKBACK WINDOW for the missing-day warning only (a game
# day inside it that no history source covers yet = one warning line,
# never an exit; the fetch itself is full-history).
HISTORY_WINDOW_DAYS = int(os.environ.get("ML_HISTORY_WINDOW_DAYS", "60"))

ARTIFACT_STALE_DAYS = 7

# Columns the run consumes from the published history (Player-Snapshots-
# backtrack-{ENV}): the frame's prior-games context (scored labels for
# prevN_goals/games_since_goal, tims/rate/lineup fields, club identity).
# tgpg/otga/otshga are NOT here: on the player table they are null
# placeholders (build_frame drops and re-merges them from Team-Stats).
# The 14 published rolling columns are deliberately NOT fetched either:
# today's shot block is RECOMPUTED by the shared training core over the
# published PER-GAME RAW columns (game_goals / game_shots / game_toi_min -
# the 20261027_add_per_game_raw_columns migration adds them; see
# shot_block_from_history). Stage-1 "last published row" carry-forward is
# GONE: the inference path is self-sufficient from Supabase.
HISTORY_FETCH_COLUMNS = [
    "date",
    "player_id",
    "name",
    "team_name",
    "team_abbrev",
    "opponent_abbrev",
    "home",
    "tims",
    "scored",
    "gpg",
    "hgpg",
    "five_gpg",
    "hppg",
    "ppg",
    "injury_status",
    "injury_desc",
    "lineup_unit",
    "lineup_position_group",
    "lineup_status",
    "pp_unit",
]

TEAM_FETCH_COLUMNS = ["date", "team_abbrev", "team_name", "tgpg", "otga", "otshga", "season", "game_id"]
GOALIE_FETCH_COLUMNS = [
    "date",
    "player_id",
    "name",
    "team_abbrev",
    "team_name",
    "gaa",
    "save_pct",
    "record",
    "shutouts",
    "games_played",
    "season",
]

# The live snapshot's own context columns (Player-Snapshots-{ENV}): the
# morning-fresh rates, club place name and confirmed opponent goalie the
# frame's today-rows are built from. A snapshot missing any of these is a
# real problem - hard fail naming exactly what is missing (the same
# fail-closed contract the 16-column check had on the old backtrack read).
REQUIRED_LIVE_COLUMNS = [
    "date",
    "player_id",
    "name",
    "team_name",
    "opp_goalie_team",
    "opp_goalie_name",
    "home",
    "tims",
    "gpg",
    "hgpg",
    "five_gpg",
    "hppg",
    "tgpg",
    "otga",
    "otshga",
    "opp_goalie_gaa",
    "opp_goalie_save_pct",
    "opp_goalie_record",
    "opp_goalie_shutouts",
    "opp_goalie_games_played",
    "injury_status",
    "injury_desc",
    "lineup_unit",
    "lineup_position_group",
    "lineup_status",
    "pp_unit",
]

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


def live_player_table(env):
    """TODAY's rows live here: Player-Snapshots-{ENV} - the daily snapshot
    the pipeline's snapshot build publishes each morning. NOT the backtrack
    tables, which only gain a date's rows after that day's games end and the
    publish runs (they are history - see history_rows)."""
    return f"Player-Snapshots-{env}"


def today_live_rows(env, on_date):
    """TODAY's prediction rows, read-only from the LIVE snapshot table.

    Returns None (prints one warning, never raises) when the table has no
    rows for ``on_date``: the morning snapshot build has not run yet, so
    there is nothing to score and the run exits CLEANLY (warn + no picks
    written + status 0). The retired contract raised RuntimeError here,
    which took the whole scheduled job down whenever the snapshot build
    lagged the 05:30 UTC picks cron.
    """
    rows = SUPABASE_ADMIN_CLIENT.table(live_player_table(env)).select("*").eq("date", on_date).execute().data or []
    if not rows:
        print(
            f"WARNING: no live snapshot rows for {on_date} in {live_player_table(env)} "
            "- snapshot build has not run yet; no picks computed",
            flush=True,
        )
        return None
    return pd.DataFrame(rows)


def require_live_columns(today, on_date):
    """The live snapshot's context contract (REQUIRED_LIVE_COLUMNS), fail-closed
    and naming exactly what is missing - the shape the 16-column check had."""
    missing = [col for col in REQUIRED_LIVE_COLUMNS if col not in today.columns]
    if missing:
        raise RuntimeError(
            f"{live_player_table(ENV)} rows for {on_date} lack the context columns the "
            f"ML pick pass consumes: missing exactly {missing}. "
            "The daily snapshot build published a malformed slice."
        )


def _paged_date_window(table, columns, lo, hi, order_cols, chunk=model.PAGE):
    """date-bounded, month-windowed, offset-paged read of one Supabase table
    (read-only). The month windows are model.fetch_table's pattern: a single
    ordered range over 600k+ rows trips PostgREST's statement timeout (57014).

    Half-open on ``hi`` by construction: the pick date's own rows never leak
    into a history fetch - a backtrack row FOR the pick date is post-game by
    construction (the publish runs after that day's games) and must not enter
    pre-game features. (model._month_windows' last window runs to the month's
    end, so each window's hi is clamped to ``hi`` here.)
    """
    frames = []
    for win_lo, month_hi in model._month_windows(lo, hi):
        win_hi = min(month_hi, hi)
        if win_lo >= win_hi:
            continue
        query = SUPABASE_ADMIN_CLIENT.table(table).select(",".join(columns)).gte("date", win_lo).lt("date", win_hi)
        for col in order_cols:
            query = query.order(col)
        offset = 0
        while True:
            rows = query.range(offset, offset + chunk - 1).execute().data
            if not rows:
                break
            frames.append(pd.DataFrame(rows))
            offset += len(rows)
    if not frames:
        return pd.DataFrame(columns=columns)
    frame = pd.concat(frames, ignore_index=True)
    return frame.sort_values(order_cols).reset_index(drop=True)


def _first_published_date(table):
    first = SUPABASE_ADMIN_CLIENT.table(table).select("date").order("date").limit(1).execute().data
    if not first:
        raise RuntimeError(f"{table} is empty - wrong --env, or nothing was published to it")
    return first[0]["date"]


def history_team_rows(env, before_date):
    """Team-Stats-backtrack-{ENV} history, strictly before the pick date: the
    club rates (tgpg/otga/otshga) at the training grain AND the game-day
    schedule add_derived computes rest days from."""
    table = model.team_stats_table(env)
    frame = _paged_date_window(
        table, TEAM_FETCH_COLUMNS, _first_published_date(table), before_date, ["date", "team_abbrev"]
    )
    return frame


def history_goalie_rows(env, before_date):
    """Goalie-Snapshots-backtrack-{ENV} history, strictly before the pick
    date (the season-to-date goalie rates the training frame merges; the
    pick date's own opponent-goalie row is injected from the live snapshot
    in _inject_today_context)."""
    table = model.goalie_table(env)
    return _paged_date_window(
        table, GOALIE_FETCH_COLUMNS, _first_published_date(table), before_date, ["date", "player_id"]
    )


def warn_missing_history_days(history, on_date):
    """ONE warning for a history tail gap - and CONTINUE.

    A missing published day is NORMAL and, since the stage-2 raw columns
    landed, should also be RARE: the ml-picks job's FIRST step
    (backfill_missing_days.py) tops up every FINAL completed game day after
    the last published date straight from the NHL API (2-4 requests per
    missing day, FINAL-state games only). This witness reads only what the
    history fetch already returned - no store, no extra requests:

    * published rows are lacking a per-game raw value on a date before the
      pick date within the lookback window (a row predating the top-up);
    * the newest published date is more than one day before the pick date
      (the backfill step did not run or could not cover the gap).

    A prior game missing raw data cannot enter the shift(1)/rolling math, so
    the affected players' features are computed over the games that ARE
    published - exact w.r.t. those records, one warning line, never an exit.
    """
    window_lo = (datetime.date.fromisoformat(on_date) - datetime.timedelta(days=HISTORY_WINDOW_DAYS)).isoformat()
    if history.empty:
        print(
            f"WARNING: no published history rows at all for {on_date} - every rolling/career "
            f"feature will be its season-first value; continuing",
            flush=True,
        )
        return
    published = history["date"].astype(str)
    raw_null = published[history["game_goals"].isna() | history["game_shots"].isna() | history["game_toi_min"].isna()]
    raw_null_days = sorted(set(raw_null.loc[raw_null >= window_lo]))
    if raw_null_days:
        print(
            f"WARNING: {len(raw_null_days)} published game day(s) inside the lookback window carry no "
            f"per-game raw stat(s) (pre-top-up rows): {raw_null_days[:5]}... - those games cannot enter "
            "the rolling/career math for the players on them; continuing with the published record",
            flush=True,
        )
    newest = published.max()
    if newest < (datetime.date.fromisoformat(on_date) - datetime.timedelta(days=1)).isoformat():
        print(
            f"WARNING: published history ends at {newest}; completed game day(s) after that are in no "
            "history source (backfill_missing_days did not cover them) - the rolling/career block for "
            f"{on_date} is computed without them; continuing",
            flush=True,
        )


def shot_block_from_history(on_date, player_ids, history):
    """The 14 shot/TOI features for ``on_date``, computed EXACTLY from the
    published per-game record in Supabase - no local store, no re-fetch.

    The stage-2 raw columns turn every published Player-Snapshots-backtrack
    row into one per-player-game line (game_goals / game_shots /
    game_toi_min, decimal minutes), so the EXACT same pandas core
    (model.shot_features_from_games - imported, never forked) can run over
    it: sorted (player_id, date, season), shift(1), season cumsums, last-5/10
    rollings, ewm(halflife=5) and the career expanding means. The season
    label rides the date (season_label - the store keyed player_games on the
    same fact). ``career_*`` are expanding sums over the full fetched
    history, which the history fetch pulled whole for precisely this.

    Players with no published row for the pick date get ONE synthetic
    zero-line (its own zeros can never enter its features - every definition
    is shifted one game), exactly the synthetic-row semantics the raw-store
    path ran. Published rows whose per-game raw stat is null are KEPT and
    fed through the core verbatim (to_numeric/_toi_minutes produce NaN,
    exactly as a store row with a null toi does) - dropping them would shift
    that player's series and break parity - so a null-raw row is a WARNING
    naming the cost (the publish_shot_cols top-up closes it), never a silent
    value change.

    Returns today's rows only: (date, player_id, 14 features).
    """
    games = history[["date", "player_id", *RAW_COLUMNS]].copy()
    incomplete = int(
        (history["game_goals"].isna() | history["game_shots"].isna() | history["game_toi_min"].isna()).sum()
    )
    if incomplete:
        print(
            f"shot-block: {incomplete} published row(s) carry an incomplete per-game raw stat (a pre-top-up "
            "row); they are fed through the core verbatim (NULL, the store path's own semantics for its "
            "null-toi rows) - the rolling cells they touch are NaN, and the one-shot top-up closes them",
            flush=True,
        )
    games = games.rename(columns={"game_goals": "goals", "game_shots": "shots", "game_toi_min": "toi"})
    games["season"] = games["date"].map(season_label)
    games["player_id"] = pd.to_numeric(games["player_id"])
    have = set(games.loc[games["date"] == on_date, "player_id"])
    synthetic_ids = [pid for pid in dict.fromkeys(player_ids) if pid not in have]
    synthetic = pd.DataFrame(
        {
            "player_id": synthetic_ids,
            "date": on_date,
            "season": season_label(on_date),
            "goals": 0,
            "shots": 0,
            "toi": 0,
        }
    )
    block = model.shot_features_from_games(pd.concat([games, synthetic], ignore_index=True))
    block = block[block["date"] == on_date].reset_index(drop=True)
    missing = [col for col in model.SHOT_FEATURES if col not in block.columns]
    if missing:
        raise RuntimeError(f"shot-block core produced no column(s) {missing} for {on_date}")
    return block


def season_label(on_date):
    """The NHL season id ('20262027') a slate date belongs to: the season
    that started the prior October runs through June, so Jan-Jun dates label
    to year-1 (verified against player_games: 2014-06-13 -> '20132014')."""
    day = datetime.date.fromisoformat(on_date)
    start = day.year if day.month >= 7 else day.year - 1
    return f"{start}{start + 1}"


# ------------------------------------------------- published per-game data


RAW_COLUMNS = ["game_goals", "game_shots", "game_toi_min"]
RAW_COLUMNS_MIGRATION = "supabase/migrations/20261027_add_per_game_raw_columns_player_snapshots_backtrack.sql"

_RAW_PROBE: dict[str, bool] = {}


def raw_columns_available(env):
    """True once the stage-2 per-game raw migration exists on this table.

    Same probe pattern publish_shot_cols.columns_landed uses (PostgREST 400s
    any request naming an unknown column). Cached per env within the run.
    """
    if env not in _RAW_PROBE:
        try:
            SUPABASE_ADMIN_CLIENT.table(model.player_table(env)).select("game_shots").limit(1).execute()
            _RAW_PROBE[env] = True
        except Exception:  # noqa: BLE001 - any failure means "the DDL is not there yet"
            _RAW_PROBE[env] = False
    return _RAW_PROBE[env]


def history_rows(env, before_date):
    """FULL prior history from Player-Snapshots-backtrack-{env}, STRICTLY
    before the pick date (post-game rows for the pick date itself never
    enter pre-game features), via the month-window pagination model.fetch_table
    uses. Full history ON PURPOSE: career_games / career_goals_pg /
    career_shots_pg are expanding career sums and ewm_* is career-long, so a
    windowed fetch would mis-state every veteran's block - and with the
    stage-2 raw columns landed, these published rows ARE the per-game series
    the exact recompute runs on."""
    table = model.player_table(env)
    columns = list(HISTORY_FETCH_COLUMNS)
    if not raw_columns_available(env):
        raise RuntimeError(
            f"{table} lacks the per-game raw columns (game_goals/game_shots/game_toi_min) the "
            "exact rolling/career recompute consumes - apply the stage-2 migration first: "
            "supabase/migrations/20261027_add_per_game_raw_columns_player_snapshots_backtrack.sql "
            "then run the publish_shot_cols.py top-up to fill history"
        )
    columns += RAW_COLUMNS
    frame = _paged_date_window(table, columns, _first_published_date(table), before_date, ["date", "player_id"])
    missing = [col for col in columns if col not in frame.columns]
    if missing:
        raise RuntimeError(f"{table} fetch lacks expected column(s) {missing}")
    return frame


# ----------------------------------------------------- today-frame assembly


def _place_abbrev_map():
    """place name -> [team_abbrev]; the New York place holds BOTH clubs
    (team_map.py) and needs the caller's context to disambiguate."""
    place_map = {}
    for abbrev, place in TEAM_ABBREV_TO_PLACE.items():
        place_map.setdefault(place, []).append(abbrev)
    return place_map


# The abbreviations that SHARE a place name (NYR + NYI both render "New
# York") - the rows whose club needs fixture/history context to resolve.
PLACE_AMBIGUOUS = {abbrev for abbrs in _place_abbrev_map().values() if len(abbrs) > 1 for abbrev in abbrs}


def _map_live_rows(today, history, goalies):
    """Map the live snapshot rows onto the training-frame shape.

    The live table publishes the club as a PLACE name (team_name - "New York"
    for both NYI and NYR, team_map.py) and the opponent as an abbrev
    (opp_goalie_team); the frame needs team_abbrev / opponent_abbrev. The
    ambiguous place resolves in order: the opponent's own abbrev (a club
    never plays itself), the opponent goalie's latest published club, the
    player's own latest published club. Still-unresolvable rows raise,
    naming the row - a club identity you cannot establish is a real snapshot
    problem, and silently guessing would corrupt opponent-goalie features.
    """
    place_map = _place_abbrev_map()
    goalie_clubs = (
        goalies.dropna(subset=["name"]).sort_values("date").groupby("name")["team_abbrev"].last().to_dict()
        if not goalies.empty
        else {}
    )
    own_clubs = (
        history.dropna(subset=["team_abbrev"]).sort_values("date").groupby("player_id")["team_abbrev"].last().to_dict()
        if not history.empty
        else {}
    )

    def _candidates(row):
        candidates = place_map.get(row["team_name"], [])
        if len(candidates) == 1:
            return candidates[0]
        opp = row.get("opponent_abbrev")
        not_opp = [c for c in candidates if c != opp]
        if len(not_opp) == 1:
            return not_opp[0]
        goalie_club = goalie_clubs.get(row.get("opp_goalie_name"))
        if goalie_club in candidates:
            return goalie_club
        own_club = own_clubs.get(row["player_id"])
        if own_club in candidates:
            return own_club
        return None

    today = today.copy()
    today["opponent_abbrev"] = today["opp_goalie_team"]
    today["team_abbrev"] = today.apply(_candidates, axis=1).astype("object")  # object: NaN survives the second pass
    # Second pass - the FIXTURE pins the ambiguous place: every resolved row
    # (club C, opponent A) proves C and A play each other tonight, so a
    # "New York" row whose opponent is C must be the A side (and, the mirror
    # case, the opponent itself is the other New York club). Covers a sparse
    # history tail (a veteran whose own latest published row predates the
    # sparse recent days) where the opponent is NOT itself a New York club.
    resolved = today[today["team_abbrev"].notna()]
    fixture_pairs = {
        (club, opp) for club, opp in resolved[["team_abbrev", "opponent_abbrev"]].dropna().itertuples(index=False)
    }
    unresolved = today["team_abbrev"].isna()
    if unresolved.any():

        def _fixture_club(row):
            candidates = place_map.get(row["team_name"], [])
            if len(candidates) < 2:
                return row["team_abbrev"]
            opp = row.get("opponent_abbrev")
            for club, opp_seen in fixture_pairs:
                if club == opp and opp_seen in candidates and opp_seen != opp:
                    return opp_seen  # the opponent plays this New York club tonight
                if opp_seen == opp and club in candidates and club != opp:
                    return club  # the opponent's own rows name this club as their opponent
            return row["team_abbrev"]

        today.loc[unresolved, "team_abbrev"] = today[unresolved].apply(_fixture_club, axis=1)
    still = today[today["team_abbrev"].isna()]
    if not still.empty:
        row = still.iloc[0]
        raise RuntimeError(
            f"cannot resolve team_name {row['team_name']!r} to a team_abbrev for player "
            f"{row['player_id']} (opponent {row.get('opponent_abbrev')!r}, opp goalie "
            f"{row.get('opp_goalie_name')!r})"
        )
    return today


def _inject_today_context(teams, goalies, today, on_date):
    """One Team-Stats / Goalie-Snapshots row per today's fixture, so
    build_frame's season / club-rate / opponent-goalie merges see the pick
    date. The backtrack tables only gain a date's rows post-game; the pick
    date's context comes from the live snapshot itself: each club's own
    published morning rates (tgpg/otga/otshga - identical on every row of
    the club) and the confirmed opponent goalie it names. add_derived then
    computes the pick date's rest days from the resulting schedule with the
    SAME math the training rows ran."""
    season = season_label(on_date)
    team_rows, goalie_rows = [], []
    for (team_name, club), rows in today.groupby(["team_name", "team_abbrev"]):
        first = rows.iloc[0]
        team_rows.append(
            {
                "date": on_date,
                "team_abbrev": club,
                "team_name": team_name,
                "tgpg": first.get("tgpg"),
                "otga": first.get("otga"),
                "otshga": first.get("otshga"),
                "season": season,
            }
        )
        opp = rows.iloc[0].get("opponent_abbrev")
        if opp and not goalies[(goalies["date"] == on_date) & (goalies["team_abbrev"] == opp)].empty:
            continue  # the opponent's goalie row is already injected
        goalie_rows.append(
            {
                "date": on_date,
                "team_abbrev": opp,
                "gaa": first.get("opp_goalie_gaa"),
                "save_pct": first.get("opp_goalie_save_pct"),
                "record": first.get("opp_goalie_record"),
                "shutouts": first.get("opp_goalie_shutouts"),
                "games_played": first.get("opp_goalie_games_played"),
            }
        )
    teams = pd.concat([teams, pd.DataFrame(team_rows)], ignore_index=True) if team_rows else teams
    goalies = pd.concat([goalies, pd.DataFrame(goalie_rows)], ignore_index=True) if goalie_rows else goalies
    return teams, goalies


def load_prediction_tables(env, on_date):
    """The run's data assembly - every read READ-ONLY.

    Returns (today, players, teams, goalies, history); ``today`` is None when
    the LIVE snapshot has no rows for the pick date (the caller exits cleanly:
    warn + no picks + status 0). ``players`` concatenates the FULL backtrack
    history (strictly before the pick date - the prior-games context) with
    today's live rows mapped onto the frame shape; ``teams``/``goalies``
    carry the history tables plus the injected pick-date context rows;
    ``history`` is the raw fetch (including the stage-2 per-game raw columns)
    that shot_block_from_history runs the exact recompute on.
    """
    today = today_live_rows(env, on_date)
    if today is None:
        return None
    require_live_columns(today, on_date)
    teams = history_team_rows(env, on_date)
    goalies = history_goalie_rows(env, on_date)
    history = history_rows(env, on_date)
    today = _map_live_rows(today, history, goalies)
    teams, goalies = _inject_today_context(teams, goalies, today, on_date)
    players = pd.concat([history, today], ignore_index=True)
    return today, players, teams, goalies, history


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
    """The former Lambda handler, unchanged in behaviour. `event` carries
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

    loaded = load_prediction_tables(ENV, on_date)
    if loaded is None:
        # CLEAN EXIT (owner rule): no LIVE snapshot rows for the pick date =
        # the morning snapshot build has not run yet and there is nothing to
        # score. Warn + no Picks write + status 0 - a scheduled run that
        # fired before the snapshot build must stay green, not traceback.
        print(f"no picks computed: no live snapshot rows for {on_date}", flush=True)
        summary_note = f"## ML picks run: {on_date}\n\n- no picks computed: no live snapshot rows for {on_date}\n"
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(summary_note)
        return {
            "statusCode": 200,
            "date": on_date,
            "picks": [],
            "dry": dry,
            "message": f"no live snapshot rows for {on_date}",
        }
    today, players, teams, goalies, history = loaded

    # TODAY's shot/TOI block: the shared training core (shot_features_
    # from_games) over the PUBLISHED per-game record in Supabase - the
    # stage-2 raw columns carry each prior game's goals/shots/toi, so the
    # rolling/career math is exact for every player the history covers. The
    # history fetch is the witness for the missing-day warning, and the
    # months/paged read already happened inside load_prediction_tables.
    shot_block = shot_block_from_history(on_date, pd.to_numeric(today["player_id"]).tolist(), history)
    warn_missing_history_days(history, on_date)
    frame = model.build_frame(players, teams, goalies, source="supabase")
    frame = frame.drop(columns=model.SHOT_FEATURES, errors="ignore").merge(
        shot_block, on=["date", "player_id"], how="left", validate="many_to_one"
    )
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
