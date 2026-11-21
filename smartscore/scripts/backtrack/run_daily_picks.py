"""Predict-only ML picks, on a scheduled GitHub Actions run: SmartScore
ensemble -> Picks-{ENV}. Replaces the retired MLPicks Lambda/image surface
(docs/ml-picks-runbook.md) - SAME inference path, $0.00 infra.

Owner rules this script implements (docs/ml-picks-runbook.md):

  * training NEVER happens here (or anywhere in CI) - it only loads a
    versioned joblib artifact that the owner's train_export.py --upload run
    pushed from the local machine;
  * TODAY's prediction rows come from the LIVE snapshot table
    Player-Snapshots-prod - the LIVE-read side is HARD-PINNED to prod (the
    owner's source-of-truth rule: the dev live snapshot shell is empty, so
    prod is the only real read; there is no --source-env knob). The
    BACKTRACK history is the ONE shared ledger
    (Player-Snapshots-backtrack-prod): the history reads (players, teams,
    goalies) always go to the -prod backtrack tables whatever the env axes
    are, because the dev backtrack tables were DELETED after prod was
    verified to supersede them (docs/backtrack_dev_deleted.md) - they gain a
    date's rows after that day's games end and the publish runs, so a
    missing recent day is NORMAL: the run prints ONE warning and continues;
  * the 14 shot/TOI features for today's rows are recomputed EXACTLY by the
    SAME pandas core model.shot_features_from_games (imported, never forked)
    over the PUBLISHED per-game record in Supabase (the stage-2 raw columns
    game_goals/game_shots/game_toi_min on Player-Snapshots-backtrack-prod,
    added by 20261027_add_per_game_raw_columns_player_snapshots_backtrack.sql)
    plus one synthetic today-row per player; the rolling/career attach's
    stage-1 "last published row" approximation is GONE from the primary path
    (it survives ONLY as carry_forward_shot_block, the clearly-marked
    last-resort fallback when the exact recompute itself raises). Published
    rows with no raw stat (pre-top-up lateness) are DROPPED from the ledger -
    the rolling/ewm/career math then runs over the available raw rows (a
    partial-prefix recompute, LOUD-warned, parity-vs-training degrades to a
    tolerance until the top-up closes the gap). The only inputs still not on
    the player table are the team/goalie rates, read at publish cadence from
    Team-Stats / Goalie-Snapshots-backtrack-prod (the same shared-ledger
    rule; a lagging one of those prints
    the runbook's staleness warning, never an exit);
    a day the backfill has not covered yet costs a WARNING, not the run;
  * STAGE B, the daily gap-fill, is the run's FIRST data step (only when NOT
    dry): backfill_missing_days.backfill tops up every FINAL completed game
    day after the table's latest published date (per-date marker rows make a
    same-day re-run a no-op skip; a dry pass stays read-only and only lists
    the missing days). A partial backfill failure is LOUD but never fatal -
    the exact recompute proceeds over the history that exists;
  * when the LIVE snapshot itself has no rows for the pick date there is
    nothing to score: the run warns ("no picks computed: no live snapshot
    rows for <date>") and exits CLEANLY (status 0, no Picks write, no
    traceback) instead of hard-failing the schedule;
  * it requires the live snapshot's own context columns (team/opponent/
    goalie/rate fields - REQUIRED_LIVE_COLUMNS) with a hard fail naming
    exactly what is missing, and asserts the built frame has the artifact's
    exact ordered feature columns;
  * WRITE contract (the owner-corrected contract, 2026-11-03): Picks-{ENV} is
    the FULL PLAYER SLATE for the current day - the site's own pipeline
    publishes it (SaveToDb -> publish_public_db -> utility.save_to_db: all
    players, their stats, wiped and reinserted by the SITE with fresh ids).
    THIS run's one and only write mode is ``stat_update``
    (stat_update_picks_db below): compute predictions for EVERY player on
    today's slate and PATCH ONLY the ``stat`` column on the existing rows
    (update().eq('id', row_id) - a PostgREST PATCH can never insert, so the
    row count, the ids and every other column are untouched; there is NO
    delete anywhere - the Picks table is NEVER wiped by this ML path; the
    table's steady state = the full slate + the model's stat column). The top-3-per-group
    challenge picks become an OUTPUT (the structured SMARTSCORE_ML_PICKS
    log + the job summary), never a table mutation. Rows the site's publish
    has not written yet (the slate does not exist in Picks-{ENV} for the
    date) are a WARN + CLEAN SKIP - we never insert our own minimal rows;
    the site's pipeline always eventually publishes the slate, and the next
    idempotent kick re-fills (the stat PATCHes are read-modify-write, so a
    repeated run for a date just re-fills the CURRENT slate). The legacy
    wipe-then-publish-the-3 mode is REMOVED ENTIRELY (its save_picks_db
    full-replace path no longer exists in this file): the site's own flows
    never needed the 3-row Picks publication (evidence:
    handle_save_historic_db derives the calendar picks via choose_picks
    from its OWN state payload, and NotifyUsers' handle_emails derives the
    email picks via choose_picks from the PUBLISHED FULL slate rows - so
    the 3-row wipe publication actively worked against both).
  * it picks one player per tims group (model.daily_picks' rule, applied by
    the ensemble's mean within-group rank percentile) for the top-3 OUTPUT.

Manual trigger: the workflow's ``date`` input (or ``--date YYYY-MM-DD``)
overrides ET-today; ``dry``/``--dry`` skips the Picks write (smoke run).

THE THREE CLI KNOBS - the module's ENTIRE accepted surface (main() below
rejects anything else) - and what is hard-pinned instead:

  * ``--date YYYY-MM-DD`` (default: ET-today) - the slate date;
  * ``--dry`` - smoke run: assemble + log the top-3 picks and the run is
    read-only end to end (no stat PATCHes, the backfill step LISTS the
    missing days without writes);
  * ``--env dev|prod`` - WHICH Picks table the stat fill targets. DEFAULT:
    the process env's ``ENV`` at import time (config.ENV,
    ``os.environ.get("ENV", "dev")``): the workflow binds the job/step
    ``ENV`` in the process environment before ``python`` starts, so an
    unset flag follows exactly the import-time semantics; an explicit
    ``--env`` rebinds the module global before run() starts (the manual/
    local override of the SAME value).

  HARD-PINS (not knobs; no flag exists for any of them):
  * write mode: ``stat_update`` ALWAYS - the single write mode; the legacy
    wipe+3-picks REPLACE is removed entirely;
  * source: ``prod`` ALWAYS (the SOURCE_ENV constant; today's rows are
    read from Player-Snapshots-prod whatever the write env);
  * features: ALWAYS ML-Daily-Features-prod (the FEATURES_TABLE constant -
    the nightly publisher's shared output, ONE table whatever the write
    env). The backtrack family is Storage-side (ml/ledger/) and its
    Postgres copies are DROPPED - there is no history read here anymore.

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
(today_live_rows / history_rows / shot_block_from_history) implement the
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
import ml_ledger  # noqa: E402  (same directory: the per-season ledger files + the shared season rule)
import model  # noqa: E402
import publish_daily_features  # noqa: E402  (same directory: the nightly publisher's published column list)
from team_map import TEAM_ABBREV_TO_PLACE  # noqa: E402

from config import ENV, SUPABASE_ADMIN_CLIENT  # noqa: E402

# The Picks-{ENV} conventions (save_to_db, utility.py) are kept as
# documentation-of-record; the write itself is the STAT-ONLY PATCH of
# stat_update_picks_db below: this run never wipes or inserts anything (the
# site's own save_to_db owns the full-slate publish).

# The SMALL feature table (publish_daily_features.py / ml_ledger.py): the
# nightly job computes the 20 history-derived features over the Storage
# ledger and publishes ~one row per slate player here, so THIS run never
# reads the big backtrack tables. Hard-pinned to -prod per the shared-ledger
# doctrine (the daily run reads ONE feature table whatever its env).
FEATURES_TABLE = os.environ.get("ML_FEATURES_TABLE", "ML-Daily-Features-prod")

ARTIFACT_STALE_DAYS = 7

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
# picks rows here are LOG/summary output only (the table write is the
# stat-only PATCH, which serializes just {"stat"}), so the whitelist keeps
# the training-frame keys for the record; REMOVE_KEYS decides what is
# actually serialized in the SMARTSCORE_ML_PICKS picks array.
REMOVE_KEYS = ["ppg"]

# ---------------------------------------------------------------- env knobs
# The CLI's EXACTLY-three knobs are --date / --dry / --env (main() below);
# everything this module used to expose as an axis is now a hard pin:
#   write   -> module-global ENV (config.ENV; the process env the workflow
#              binds), overridable ONLY by --env dev|prod: an explicit --env
#              rebinds the module global before run() starts. The unset-flag
#              default IS the import-time read (os.environ.get("ENV",
#              "dev")) - the workflow binds the step-level ENV in the
#              process environment before python starts.
#   source  -> SOURCE_ENV = 'prod' PINNED (the owner's source-of-truth
#              rule: prod is the source of truth for the live-read side;
#              the dev snapshot shell is empty).
#   features-> FEATURES_TABLE hard-pinned to -prod (ONE shared table
#              whatever the picks env; the nightly publisher owns it).
#   mode    -> stat_update ALWAYS (the module docstring's WRITE contract
#              bullet; the legacy wipe+3-picks mode is removed entirely).
ENV_CHOICES = ("dev", "prod")
SOURCE_ENV = "prod"


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

    # Calibrator (optional): a monotonic map from the scoring probability to a
    # calibrated probability. Absent on older artifacts -> inference falls back
    # to the raw mean member probability.
    calibrator = None
    if isinstance(bundle, dict):
        calibrator = bundle.get("calibrator")
        if calibrator is None and isinstance(models, dict):
            calibrator = models.get("calibrator")
    bundle["calibrator"] = calibrator
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


def require_live_columns(today, on_date, source_env=None):
    """The live snapshot's context contract (REQUIRED_LIVE_COLUMNS), fail-closed
    and naming exactly what is missing - the shape the 16-column check had.

    ``source_env`` names the LIVE-read axis (Player-Snapshots-{source_env});
    it falls back to the SOURCE_ENV hard pin ('prod') so unset callers read
    the real snapshot."""
    source_env = source_env or SOURCE_ENV
    missing = [col for col in REQUIRED_LIVE_COLUMNS if col not in today.columns]
    if missing:
        raise RuntimeError(
            f"{live_player_table(source_env)} rows for {on_date} lack the context columns the "
            f"ML pick pass consumes: missing exactly {missing}. "
            "The daily snapshot build published a malformed slice."
        )


def season_label(on_date):
    """The NHL season id ('20262027') a slate date belongs to: the season
    that started the prior October runs through June, so Jan-Jun dates label
    to year-1 (verified against player_games: 2014-06-13 -> '20132014').

    TWO COVID-era exceptions, verified against the raw store's own season
    ids (56 distinct game dates): the 2019-20 post-season was played
    July-September 2020 (the bubble - player_games keys those games
    20192020) and the delayed 2020-21 post-season ran into July 2021
    (keyed 20202021), so a plain month rule would mislabel both and shift
    every season-scoped feature for the veterans who played in them. The
    published record carries no season column, so this label IS the season
    key the recompute groups on - it must match the training path's store
    ids (verified: zero disagreements across the store's full date range).

    The implementation lives in ml_ledger.season_of - the ledger's
    per-season file split groups rows by the SAME rule, so one
    implementation serves both."""
    return ml_ledger.season_of(on_date)


# ----------------------------------------------------- today-frame assembly


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


def _inject_today_context(teams, goalies, today, on_date):
    """One Team-Stats / Goalie-Snapshots row per today's FIXTURE CLUB, so
    build_frame's season / club-rate / opponent-goalie merges see the pick
    date. The backtrack tables only gain a date's rows post-game; the pick
    date's context comes from the live snapshot itself: each club's own
    published morning rates (tgpg/otga/otshga - identical on every row of
    the club) and the confirmed opponent goalie it names. add_derived then
    computes the pick date's rest days from the resulting schedule with the
    SAME math the training rows ran.

    Grouped by CLUB, not (team_name, club): the live slate can carry more
    than one team_name spelling per club (upstream data quirks), and two
    group rows for one (date, club) would violate build_frame's
    many_to_one season join. team_name rides the first row of the club."""
    season = season_label(on_date)
    team_rows, goalie_rows = [], []
    for club, rows in today.groupby("team_abbrev"):
        first = rows.iloc[0]
        team_name = first.get("team_name")
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


def features_rows(on_date, player_ids=None):
    """ML-Daily-Features rows for the pick date - the small-table path.

    The nightly publisher (publish_daily_features.py) computes the 20
    history-derived features over the Storage ledger and publishes them
    here; when rows exist, this run reads NOTHING from the big backtrack
    tables. ``None`` = no rows for the date (or the table is not there yet
    - the pre-cutover state): the caller uses the full-ledger path, which
    is why this probe swallows a read failure with ONE printed line instead
    of failing the run.

    PAGED, and filtered to the slate's player_ids when given: a single
    un-paged PostgREST read caps at 1,000 rows - the 2026-11-21 slate's
    2,127 feature rows silently truncated at the cap and 506 live players
    (Caufield, Thompson, Barkov...) lost their features to NaN, reshuffling
    the picks. When ``player_ids`` is given the read filters with ``in_``
    AND pages, so the slate is always fully covered."""
    try:
        query = SUPABASE_ADMIN_CLIENT.table(FEATURES_TABLE).select("*").eq("date", on_date)
        if player_ids is not None:
            query = query.in_("player_id", [int(pid) for pid in player_ids])
        rows, offset = [], 0
        while True:
            page = query.range(offset, offset + 999).execute().data or []
            rows.extend(page)
            if len(page) < 1000:
                break
            offset += len(page)
    except Exception as exc:  # noqa: BLE001 - "table absent/unreadable yet" is a normal transition state
        print(
            f"WARNING: {FEATURES_TABLE} unreadable ({type(exc).__name__}: {str(exc)[:160]}) - "
            "falling back to the full-ledger history path",
            flush=True,
        )
        return None
    # Shape guard: the published table always carries the publisher's
    # identity columns. Anything else (a pre-migration table, a stubbed
    # client's default answer) is treated as "not published yet".
    if not (isinstance(rows, list) and rows and isinstance(rows[0], dict) and "club" in rows[0]):
        print(
            f"WARNING: {FEATURES_TABLE} returned an unexpected shape for {on_date} - "
            "falling back to the full-ledger history path",
            flush=True,
        )
        return None
    return pd.DataFrame(rows)


def _merge_features(today, features, on_date):
    """Attach the published feature rows to today's live rows, and resolve
    club/opp from them (the publisher resolved both from the NHL schedule -
    the same resolution _map_live_rows does against the history when the
    full-ledger path runs). Slate players with no published feature row keep
    NaN club/opp: their rates/features are NaN and daily_picks skips them -
    ONE warning names them (a surprise call-up the nightly roster pass
    missed; tomorrow's publisher run picks them up)."""
    today["player_id"] = pd.to_numeric(today["player_id"])
    # The published columns are AUTHORITATIVE (the publisher resolved
    # club/opp from the NHL schedule and computed the features over the
    # ledger): drop any same-named columns the live side carries so the
    # merge cannot suffix-collide (ppg_x/ppg_y, team_abbrev_x/_y, ...).
    # team_abbrev/opponent_abbrev are on the list too: the features path
    # replaces them with the publisher's club/opp whatever the live side
    # publishes.
    feature_cols = [
        "date",
        "player_id",
        "club",
        "opp",
        "ppg",
        *model.SHOT_FEATURES,
        *publish_daily_features.STREAK_COLUMNS,
    ]
    today = today.drop(
        columns=[c for c in feature_cols if c not in ("date", "player_id")] + ["team_abbrev", "opponent_abbrev"],
        errors="ignore",
    )
    features = features[[col for col in feature_cols if col in features.columns]].copy()
    features["player_id"] = pd.to_numeric(features["player_id"])
    features = features.rename(columns={"club": "team_abbrev", "opp": "opponent_abbrev"})
    merged = today.merge(features, on=["date", "player_id"], how="left", validate="one_to_one")
    uncovered = merged[merged["team_abbrev"].isna()]
    if not uncovered.empty:
        names = [str(n) for n in uncovered["name"].head(10)]
        print(
            f"WARNING: {len(uncovered)} slate player(s) have no published feature row for {on_date} "
            f"({', '.join(names)}{'...' if len(uncovered) > 10 else ''}) - their pre-game features are "
            "NaN and daily_picks will skip them (the nightly publisher covers the roster; a "
            "same-day call-up waits for the next pass)",
            flush=True,
        )
    return merged


def load_prediction_tables(source_env, on_date):
    """The run's data assembly - every read READ-ONLY.

    ``source_env`` is the LIVE-read axis: ``today`` is read from
    Player-Snapshots-{source_env} - the run passes the SOURCE_ENV hard pin
    ('prod').

    ONE path: ML-Daily-Features has rows for the pick date - the nightly
    publisher's output (publish_daily_features.py over the Storage ledger).
    ``today`` carries the 20 published feature columns (merged) and
    club/opp resolved from them; players/teams/goalies are the today-only
    frames with the injected pick-date context rows. The backtrack family
    is Storage-side (ml/ledger/) and its Postgres copies are DROPPED -
    there is no history read here.

    ``today`` is None when the LIVE snapshot has no rows for the pick date
    (the caller exits cleanly: warn + no picks + status 0)."""
    today = today_live_rows(source_env, on_date)
    if today is None:
        return None
    require_live_columns(today, on_date, source_env=source_env)
    features = features_rows(on_date, player_ids=pd.to_numeric(today["player_id"]).tolist())
    if features is None:
        raise RuntimeError(
            f"{FEATURES_TABLE} has no rows for {on_date} - the nightly ledger job "
            "(Actions -> ML Daily Picks -> nightly, publish_daily_features.py) has not published "
            "this date's feature rows yet. The daily run no longer reads the backtrack family "
            "(docs/ml-ledger-runbook.md); run/repair the nightly job instead of looking for a "
            "Supabase ledger that was dropped at cutover."
        )
    today = _merge_features(today, features, on_date)
    empty_teams = pd.DataFrame(columns=TEAM_FETCH_COLUMNS)
    empty_goalies = pd.DataFrame(columns=GOALIE_FETCH_COLUMNS)
    teams, goalies = _inject_today_context(empty_teams, empty_goalies, today, on_date)
    return today, today.copy(), teams, goalies, None


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
    # Actual scoring probability: mean of the members' native probabilities.
    # The ensemble percentile above remains the ranking key, so picks are
    # unchanged; only the published value moves from a rank to a probability.
    sim["prob"] = sim[["hgb", "lgbm", "logreg"]].mean(axis=1)
    calibrator = bundle.get("calibrator") if isinstance(bundle, dict) else None
    if calibrator is not None:
        try:
            if hasattr(calibrator, "predict_proba"):
                sim["prob"] = np.asarray(calibrator.predict_proba(sim[["prob"]])[:, 1], dtype=float)
            elif callable(calibrator):
                sim["prob"] = np.asarray(calibrator(sim["prob"]), dtype=float)
            elif hasattr(calibrator, "transform"):
                sim["prob"] = np.asarray(calibrator.transform(sim["prob"]), dtype=float)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: calibrator apply failed: {exc}", flush=True)
    sim["prob"] = sim["prob"].clip(lower=0.0, upper=1.0)
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


def stat_value(prob):
    """The Picks `stat` value: an actual probability of the player scoring in
    that game (0-1), shared by the SMARTSCORE_ML_PICKS top-3 output
    (pick_rows_to_picks_db's stat cell) and the stat_update computation
    stage. Returns None when the prediction is not finite (a NaN must never
    PATCH a number into the column).

    The ranking is deliberately NOT derived from this value: picks are selected
    on the ensemble rank percentile (see ``ensemble_scores``), so publishing a
    probability here cannot change which players are picked. The DB WRITE
    boundary (stat_update_picks_db) stores this value verbatim (full
    precision) - no /100 scaling, because the value is already a 0-1
    probability."""
    value = float(prob)
    if not np.isfinite(value):
        return None
    return float(np.clip(value, 0.0, 1.0))


def pick_rows_to_picks_db(picks):
    """Whitelist the top-3 picks into the shared output shape
    (PICK_DB_COLUMNS) - the picks array of the structured SMARTSCORE_ML_PICKS
    log and the job-summary table (NOT a table write: the run's only write is
    the stat-only PATCH of stat_update_picks_db).

    REMOVE_KEYS are dropped here, before anything is serialized: the output
    rows use exactly the Picks table's live column keys (see the module
    docstring - a table schema change updates the whitelist AND a migration,
    or REMOVE_KEYS).
    """
    write_keys = [key for key in PICK_DB_COLUMNS if key not in REMOVE_KEYS]
    rows = []
    for _, row in picks.iterrows():
        prob = row.get("prob", row.get("ensemble", np.nan))
        stat = stat_value(prob)
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


def _historic_pick_columns(env):
    table = f"Historic-Picks-{env}"
    try:
        rows = SUPABASE_ADMIN_CLIENT.table(table).select("*").limit(1).execute().data or []
        if rows:
            return list(rows[0].keys())
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not discover Historic-Picks columns: {exc}", flush=True)
    # Conservative fallback that exists on every Picks/Historic-Picks table.
    return ["id", "date", "player_id", "name", "team_name", "tims", "stat"]


def write_historic_picks_if_missing(picks, on_date, env):
    """Idempotently write the ML top-3 picks to Historic-Picks-{env}.

    The AWS pipeline no longer writes today's historic picks (it skips them so
    the ML pass owns them). This runs on every ML pass but only writes when the
    date is absent, so repeat kicks / the nightly cron are no-ops.
    """
    table = f"Historic-Picks-{env}"
    existing = SUPABASE_ADMIN_CLIENT.table(table).select("date").eq("date", on_date).limit(1).execute().data or []
    if existing:
        print(f"Historic-Picks-{env}: {on_date} already present - skipping write", flush=True)
        return

    if picks is None or (hasattr(picks, "empty") and picks.empty):
        print("Historic-Picks write skipped: no ML picks", flush=True)
        return

    columns = _historic_pick_columns(env)

    # Keep the table to the most recent DAYS_TO_KEEP_HISTORIC_DATA days (mirrors
    # the site's own publish pruning) so it stays a ~week visual instead of
    # growing forever. Prune before inserting today's rows.
    DAYS_TO_KEEP_HISTORIC_DATA = 8
    try:
        all_rows = SUPABASE_ADMIN_CLIENT.table(table).select("date").execute().data or []
        distinct_dates = sorted({r.get("date") for r in all_rows if r.get("date")})
        if len(distinct_dates) >= DAYS_TO_KEEP_HISTORIC_DATA:
            to_remove = distinct_dates[: len(distinct_dates) - (DAYS_TO_KEEP_HISTORIC_DATA - 1)]
            for d in to_remove:
                SUPABASE_ADMIN_CLIENT.table(table).delete().eq("date", d).execute()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: Historic-Picks pruning failed: {exc}", flush=True)

    # `id` is the table's (non-null, no-default) primary key. The site's own
    # publish renumbers every row sequentially on each write, but this ML pass
    # INSERTs brand-new rows, so a fixed 1..N would collide with the rows
    # already stored from earlier dates. Derive the next free id from the
    # current max instead (and fall back to 1..N only on an empty/odd table).
    next_id = 1
    if "id" in columns:
        try:
            existing_ids = SUPABASE_ADMIN_CLIENT.table(table).select("id").execute().data or []
            parsed = [int(r.get("id")) for r in existing_ids if r.get("id") is not None]
            if parsed:
                next_id = max(parsed) + 1
        except (TypeError, ValueError):
            next_id = 1
    records = []
    for i, (_, row) in enumerate(picks.iterrows()):
        record = {"date": on_date}
        pid = row.get("player_id", row.get("id"))
        if isinstance(pid, (np.generic,)):
            pid = pid.item()
        record["player_id"] = int(pid) if pid is not None and not (isinstance(pid, float) and np.isnan(pid)) else None
        if "id" in columns:
            record["id"] = next_id + i
        if "Scored" in columns:
            record["Scored"] = None
        # Columns whose DB type is an integer (see the Historic-Picks
        # schema: player_id, tims, Scored, opp_goalie_nhl_id,
        # opp_goalie_shutouts, opp_goalie_games_played are all integer/
        # bigint). pandas carries them as float64 whenever a NaN is
        # possible, so a value such as 10.0 would be sent verbatim and
        # Postgres rejects "10.0" for an integer column
        # ("invalid input syntax for type integer"). Coerce integral floats
        # (and numpy scalars) to a Python int before serialising.
        _INTEGER_COLUMNS = {
            "player_id",
            "tims",
            "Scored",
            "opp_goalie_nhl_id",
            "opp_goalie_shutouts",
            "opp_goalie_games_played",
        }
        for col in columns:
            if col in ("id", "date", "player_id", "Scored"):
                continue
            if col == "stat":
                value = stat_value(row.get("ensemble", np.nan))
                record[col] = value
            elif col in getattr(row, "index", []):
                value = row.get(col)
                if isinstance(value, (np.generic,)):
                    value = value.item()
                if isinstance(value, float) and np.isnan(value):
                    value = None
                if value is not None and col in _INTEGER_COLUMNS:
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        value = None
                record[col] = value
        records.append(record)

    if not records:
        return
    SUPABASE_ADMIN_CLIENT.table(table).insert(records).execute()
    print(f"Historic-Picks-{env}: wrote {len(records)} ML picks for {on_date}", flush=True)


# ------------------------------------------------------------- stat update
# The DEFAULT write mode (the owner-corrected contract, 2026-11-03): the
# Picks-{ENV} table is the FULL PLAYER SLATE the site's own pipeline
# publishes (SaveToDb -> handle_publish_db -> publish_public_db ->
# utility.save_to_db, full wipe + full-slate reinsert). This run only fills
# the `stat` column on the existing rows - the write below is a plain
# PostgREST PATCH per row (update().eq('id', row_id)), which can NEVER
# insert a row: contract-safe by construction, no probe/restore scaffolding
# needed.


def picks_slate_rows(on_date, env=None):
    """Today's Picks-{env} slate rows, read-only: (id, player_id, name,
    team_name, stat, tims) - the site's own full-slate publish produced
    them; the stat_update write PATCHes only their stat.

    Returns None when the table has NO rows for ``on_date``: the site's own
    SaveToDb publish has not written today's slate yet (the run may fire
    before the pipeline's publish in wall-clock, e.g. a manual dispatch).
    Callers must WARN + SKIP cleanly - the same contract
    ``today_live_rows`` implements on the live-read side - and NEVER insert
    their own minimal rows: the site's pipeline always eventually publishes
    the slate, and the next idempotent kick (or the nightly cron) re-fills.
    """
    env = env or ENV
    table = f"Picks-{env}"
    rows = (
        SUPABASE_ADMIN_CLIENT.table(table)
        .select("id,player_id,name,team_name,stat,tims")
        .eq("date", on_date)
        .execute()
        .data
        or []
    )
    if not rows:
        print(
            f"WARNING: Picks-{env} has no rows for {on_date} - the site's own publish "
            "(SaveToDb -> publish_public_db -> save_to_db) has not written today's full slate yet; "
            "stat fill SKIPPED cleanly (never inserting our own rows - the site's pipeline will "
            "publish the slate, and the next kick re-fills it)",
            flush=True,
        )
        return None
    return rows


def stat_update_picks_db(sim, on_date, env=None):
    """stat_update - THE one and only write: PATCH ONLY the `stat` column on
    today's Picks-{ENV} slate.

    The owner-corrected contract (module docstring, WRITE contract): the table
    is the FULL PLAYER SLATE the site's own pipeline publishes; this run
    fills the stat column on EVERY player of the slate the model could
    score, and touches NOTHING else:

      1. read the existing rows (picks_slate_rows above - a missing slate is
         a WARN + clean skip, never a write);
      2. compute every slate row's stat from ``sim`` (stat_value, the same
         convention the top-3 output row serialization uses) and PATCH it,
         divided by 100 at this write boundary - the column carries the
         0-1 decimal form (a 100th-percentile player writes exactly 1.0;
         full precision, no re-rounding), while the log/picks display stays
         on the 0-100 scale: ``update({'stat': v}).eq('id', row_id)``. A
         PostgREST PATCH
         filtered by an existing id can never insert and never touches
         another column - so the row count, the ids and every other column
         are identical before/after by construction (test-pinned); the
         per-player PATCH keeps the row's OWN id (never renumbered - the
         ML path NEVER wipes this table);
      3. WARN by name for slate rows the model frame could not score
         (call-ups without history) and leave their stat UNTOUCHED - no
         invented values, no inserted rows;
      4. WARN for model frame players the slate does not carry (likely not
         playing tonight) and never write them.

    Idempotent by construction (a read-modify-write per row): a repeated
    run for a date simply re-fills the CURRENT slate. A slate row whose
    date vanished mid-run (the site re-published between step 1 and 2) is a
    silent no-op PATCH (eq matched nothing) - never a new row.

    A failed PATCH is LOUD (one line with the failing id) and the loop
    continues, so one bad row cannot withhold the rest of the fill; when
    EVERY patch fails the run raises - a green run with zero writes can
    only be the slate-missing skip above; the PATCH needs no wipe/restore
    scaffolding - it can never delete or insert anything.

    Returns the counts: {"slate_rows", "filled", "no_frame", "not_on_slate", "skipped"}.
    """
    env = env or ENV
    table = f"Picks-{env}"
    slate = picks_slate_rows(on_date, env=env)
    if slate is None:
        return {"slate_rows": 0, "filled": 0, "no_frame": 0, "not_on_slate": 0, "skipped": True}

    if "prob" in getattr(sim, "columns", []):
        prob_series = sim["prob"]
    elif all(col in getattr(sim, "columns", []) for col in ("hgb", "lgbm", "logreg")):
        prob_series = sim[["hgb", "lgbm", "logreg"]].mean(axis=1)
    else:
        prob_series = sim["ensemble"]
    predictions = {
        int(player_id): (stat if (stat := stat_value(prob)) is not None else None)
        for player_id, prob in zip(sim["player_id"], prob_series)
        if pd.notna(player_id)
    }
    slate_ids = set()
    filled_id2stat = []  # (id, stat)
    no_frame = []
    for row in slate:
        try:
            player_id = int(row.get("player_id"))
        except (TypeError, ValueError):
            no_frame.append(f"{row.get('name')!r} (player_id {row.get('player_id')!r}: not numeric)")
            continue
        slate_ids.add(player_id)
        if player_id not in predictions:
            no_frame.append(f"{row.get('name')!r} (player_id {player_id})")
            continue
        filled_id2stat.append((row.get("id"), predictions[player_id]))

    # Frame players absent from the slate: the site did not publish them
    # tonight (scratch/healthy-scratch/not dressed). WARN, never write.
    not_on_slate = [pid for pid in predictions if pid not in slate_ids]
    _warn_pids = ", ".join(str(pid) for pid in not_on_slate[:10])
    if not_on_slate:
        print(
            f"WARNING: {len(not_on_slate)} model frame player(s) (player_id(s) {_warn_pids}"
            f"{'...' if len(not_on_slate) > 10 else ''}) are not on today's Picks-{env} slate - "
            "likely not playing tonight; no stat write for them",
            flush=True,
        )
    if no_frame:
        print(
            f"WARNING: {len(no_frame)} Picks-{env} slate row(s) have no model prediction (call-ups "
            f"without published history or a bad player_id) - stat left UNTOUCHED: "
            f"{no_frame[:10]}{'...' if len(no_frame) > 10 else ''}",
            flush=True,
        )

    # One PATCH per row: only the stat column moves; a non-finite prediction
    # PATCHes None (the column keeps whatever the site published) too.
    fill_failures = []
    filled = 0
    for row_id, stat in filled_id2stat:
        if row_id is None:
            fill_failures.append(("(no id)", stat))
            continue
        try:
            SUPABASE_ADMIN_CLIENT.table(table).update({"stat": stat}).eq("id", row_id).execute()
            filled += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not withhold the fill
            fill_failures.append((row_id, None))
            print(
                f"WARNING: stat PATCH failed for Picks-{env} row id={row_id} ({type(exc).__name__}: "
                f"{str(exc)[:160]}) - the row's stat is untouched (idempotent: the next kick re-fills)",
                flush=True,
            )
    if slate and not filled and predictions and not no_frame:
        raise RuntimeError(
            f"stat_update wrote zero stat columns on Picks-{env} for {on_date} while "
            f"{len(slate)} slate row(s) had predictions - the table write path failed outright"
        )
    if fill_failures and filled:
        print(
            f"WARNING: {len(fill_failures)} of {len(filled_id2stat)} stat PATCH(es) failed; the "
            f"remaining {filled} landed (the failing rows keep the site's stat values)",
            flush=True,
        )
    print(
        f"stat_update: filled stat on {filled}/{len(slate)} Picks-{env} slate row(s) "
        f"(no prediction for {len(no_frame)}, not on slate: {len(not_on_slate)}) - "
        "all other columns and rows untouched",
        flush=True,
    )
    result = {
        "slate_rows": len(slate),
        "filled": filled,
        "no_frame": len(no_frame),
        "not_on_slate": len(not_on_slate),
        "skipped": False,
    }
    return result


# ------------------------------------------------------------------ run


def today_et():
    import pytz  # noqa: PLC0415  (lazy; single call per run)

    return datetime.datetime.now(pytz.timezone("America/Toronto")).strftime("%Y-%m-%d")


def job_summary(
    on_date,
    key,
    meta,
    n_picks,
    mean_max_prob,
    dry,
    *,
    stat_result=None,
    pick_rows=None,
):
    """Append the run's headline to $GITHUB_STEP_SUMMARY when one exists
    (on GitHub Actions); a no-op anywhere else."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        # LOUD: this silent return made a broken workflow wiring invisible -
        # every historical run said "the pass did not reach job_summary"
        # while the real reason (an unset env var, a failed write) stayed
        # hidden. The next run's log names it.
        print("summary: GITHUB_STEP_SUMMARY is not set - nothing appended (outside Actions?)", flush=True)
        return
    lines = [
        f"## ML picks run: {on_date}",
        "",
        "- mode: stat_update (hard-pinned); source: prod; features: ML-Daily-Features-prod",
        f"- artifact: `{key}` (train_through {meta.get('train_through')}, seed {meta.get('seed')})",
        f"- top-3 challenge picks (OUTPUT ONLY - not written to the Picks table): {n_picks}",
        "- stat column updated on ALL players of the current Picks-{ENV} slate (no wipe; "
        "the site's SaveToDb owns the slate rows)",
        f"- mean ensemble percentile: {mean_max_prob}",
    ]
    if dry:
        lines.append("- DRY run: no stat PATCHes (read-only)")
    elif stat_result is not None:
        if stat_result["skipped"]:
            fill_line = "- stat fill: SKIPPED cleanly (the slate has not been published yet)"
        else:
            fill_line = (
                f"- stat fill: {stat_result['filled']} of {stat_result['slate_rows']} slate row(s) patched; "
                f"{stat_result['no_frame']} slate row(s) left untouched (no prediction); "
                f"{stat_result['not_on_slate']} frame player(s) not on the slate (no write)"
            )
        lines.append(fill_line)
    lines += [
        "",
        "| tims | name | team | stat |",
        "| --- | --- | --- | --- |",
    ]
    for row in pick_rows or []:
        lines.append(f"| {row.get('tims')} | {row.get('name')} | {row.get('team_name')} | {row.get('stat')} |")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print(f"summary: appended {len(lines)} headline line(s) to {path}", flush=True)


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

    1. a local artifact path (``ML_ARTIFACT_PATH``, or the event's
       ``artifact_path`` for programmatic callers) - always wins;
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
        "no artifact source: set ML_STORAGE_BUCKET for the "
        "default Supabase Storage read, set ML_S3_BUCKET for the "
        "S3 read, or set ML_ARTIFACT_PATH (the local fallback)"
    )


def run(event=None, context=None):
    """The former Lambda handler, same shape. `event` carries optional
    date / dry / artifact_path / s3_bucket / s3_prefix (+ s3/storage
    storage_prefix overrides). There are NO mode/source/history keys: stat
    is the only write mode, source and history are hard-pinned (SOURCE_ENV
    'prod', the ONE shared -prod ledger). The WRITE target is the module
    global ENV (config.ENV; the CLI's --env rebinds it). `context` is kept
    in the signature so the lifted test surface stays drop-in."""
    event = event or {}
    on_date = str(event.get("date") or today_et())
    dry = bool(event.get("dry"))
    # NO AXES, NO MODE RESOLUTION: every former axis is a hard pin
    # (module docstring, HARD-PINS). One loud identity line greets the log:
    print(
        f"mode: stat_update (hard-pinned); source: prod (Player-Snapshots-{SOURCE_ENV}); "
        f"features: {FEATURES_TABLE}; write: Picks-{ENV}",
        flush=True,
    )

    bucket = event.get("s3_bucket") or os.environ.get("ML_S3_BUCKET")
    prefix = event.get("s3_prefix") or os.environ.get("ML_ARTIFACT_PREFIX", "predict/ml")
    storage_bucket = event.get("storage_bucket") or os.environ.get("ML_STORAGE_BUCKET")
    storage_prefix = event.get("storage_prefix") or os.environ.get("ML_ARTIFACT_STORAGE_PREFIX", "predict/ml")
    # The local-file overlay keeps its env-var source (the --artifact flag is
    # gone with the rest of the removed surface): ML_ARTIFACT_PATH - or an
    # event artifact_path for programmatic callers - always wins.
    artifact_path = event.get("artifact_path") or os.environ.get("ML_ARTIFACT_PATH")

    key, bundle = pick_daily_bundle(bucket, prefix, artifact_path, storage_bucket, storage_prefix)
    meta = bundle["metadata"]
    stale = staleness_note(bundle)
    print(
        f"artifact {key}: through={meta.get('train_through')} seed={meta.get('seed')} "
        f"git={meta.get('git_hash')} age={stale}d",
        flush=True,
    )

    # NO gap-fill here: the nightly ledger job owns it (publish_daily_
    # features.py backfills the Storage ledger and publishes the feature
    # rows this run reads). The backtrack family is Storage-side; the
    # Postgres copies were dropped at cutover.

    # The LIVE read: Player-Snapshots-prod, HARD-PINNED (SOURCE_ENV); the
    # history reads route to the ONE shared -prod ledger (HISTORY_ENV +
    # 027ef47's shared-ledger rule). The backfill keeps its own --env-arg
    # call shape, but targets the shared ledger either way.
    loaded = load_prediction_tables(SOURCE_ENV, on_date)
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

    # The nightly publisher's block: the 14 shot/TOI columns already rode
    # _merge_features onto today - extract them in the same shape the merge
    # below consumes (build_frame's own published columns are replaced with
    # these; same values, one source of truth: the published feature row).
    shot_block = today[["date", "player_id", *model.SHOT_FEATURES]].copy()
    frame = model.build_frame(players, teams, goalies, source="supabase")
    frame = frame.drop(columns=model.SHOT_FEATURES, errors="ignore").merge(
        shot_block, on=["date", "player_id"], how="left", validate="many_to_one"
    )
    # prevN/rest came published; the same-day projected-units merge and the
    # rank percentiles are all that is left to compute here.
    frame = model.add_rank_features(model.add_projected_lineup_features(frame))
    frame = frame[frame["date"].astype(str) == on_date].reset_index(drop=True)
    if not set(bundle["all_features"]).issubset(frame.columns):
        missing = [c for c in bundle["all_features"] if c not in frame.columns]
        raise RuntimeError(f"frame build lacks artifact features {missing}; shape/order mismatch")
    if frame.empty:
        raise RuntimeError(f"frame produced no rows for {on_date}")

    sim = ensemble_scores(bundle, frame)

    # THE WRITE (no mode branch: stat_update is the ONLY mode - the module
    # docstring's WRITE contract). It runs BEFORE the top-3 assembly so a
    # malformed picks shape can never withhold the stat fill (the fill is
    # the run's primary output; the top-3 computation then fails/validates
    # exactly as it always did).
    stat_result = None
    if dry:
        print(f"dry: stat update skipped (read-only) - the Picks-{ENV} slate would have been stat-filled", flush=True)
    else:
        stat_result = stat_update_picks_db(sim, on_date)

    picks = model.daily_picks(sim, "ensemble")
    validate_picks(picks, on_date)
    rows = pick_rows_to_picks_db(picks)
    if not dry:
        try:
            write_historic_picks_if_missing(picks, on_date, ENV)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: Historic-Picks write failed: {exc}", flush=True)
    mean_max_prob = round(float(pd.to_numeric(picks["ensemble"], errors="coerce").mean()), 4) if len(picks) else None
    stat_payload = {}
    if stat_result is not None:
        stat_payload |= {
            "slate_rows": stat_result["slate_rows"],
            "stat_filled": stat_result["filled"],
            "no_frame_skipped": stat_result["no_frame"],
            "not_on_slate_skipped": stat_result["not_on_slate"],
            "stat_fill_skipped": stat_result["skipped"],
        }
    elif dry:
        stat_payload |= {"stat_fill_skipped": True, "reason": "dry"}
    print(
        json.dumps(
            {
                "log": "SMARTSCORE_ML_PICKS",
                "date": on_date,
                "n_picks": len(rows),
                "picks": [
                    {
                        "player_id": int(row["player_id"]),
                        "name": row["name"],
                        "team_name": row["team_name"],
                        "tims": int(row["tims"]),
                        "stat": row["stat"],
                    }
                    for row in rows
                ],
                "stat_column": stat_payload,
                "mean_max_prob": mean_max_prob,
                "stat_scale": "scoring probability (mean member predict_proba)",
                "write_env": ENV,
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
        dry_note = f"assembled {len(rows)} ML picks (top-3 output) for {on_date} (stat fill deferred to a live pass)"
        print(f"dry: {dry_note}", flush=True)
    job_summary(
        on_date,
        key,
        meta,
        len(rows),
        mean_max_prob,
        dry,
        stat_result=stat_result,
        pick_rows=rows,
    )

    result = {
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
    if stat_result is not None:
        result["stat_update"] = stat_result
    return result


# Kept as the module's canonical entry name so callers (and tests) get the
# same interface the Lambda handler exposed; run() is the same function.
handler = run


def main(argv=None):
    """The CLI: EXACTLY three knobs (module docstring, THE THREE CLI KNOBS) -
    --date, --dry, --env. Nothing else is accepted (argparse exits 2 on any
    other flag, e.g. the removed --write-mode/--source-env/--history-env).
    Source (prod) and history (the shared ledger) are hard pins, not knobs."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=None, help="slate date YYYY-MM-DD (default: ET-today)")
    parser.add_argument("--dry", action="store_true", help="smoke run: assemble picks, skip the Picks write")
    parser.add_argument(
        "--env",
        choices=ENV_CHOICES,
        default=None,
        help="WHICH Picks table the stat fill targets (dev|prod). DEFAULT: the process env's ENV at "
        "import time (config.ENV, os.environ.get('ENV', 'dev') - the workflow binds the job/step env "
        "before python starts); an explicit --env rebinds that same module value before run() starts. "
        "SOURCE (prod) and HISTORY (the shared ledger) are hard-pinned - not knobs",
    )
    args = parser.parse_args(argv)

    event = {}
    if args.date:
        event["date"] = args.date
    if args.dry:
        event["dry"] = True
    if args.env:
        # The --env override rebinds the module-global write env the writer,
        # the gap-fill and the live-table helpers all capture - a single,
        # unambiguous, process-local rebind before run() starts (the unset
        # default stays config.ENV's import-time read).
        global ENV  # noqa: PLW0603  (the same module-global rebind validate.py --env does)
        ENV = args.env
    return run(event)


if __name__ == "__main__":
    main()
