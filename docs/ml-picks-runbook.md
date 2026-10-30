# ML picks runbook (scheduled GitHub Actions; MLPicks Lambda retired)

Replaces the retired `docs/aws-predict-lambda-runbook.md`. Authored against
`branch feat/final-clean` and verified against
`smartscore/scripts/backtrack/run_daily_picks.py` (the former Lambda
handler's logic, unchanged),
`.github/workflows/ml-picks.yml`, `smartscore/utility.py` (save_to_db via
publish_public_db), `smartscore/scripts/backtrack/train_export.py`, and
`smartscore/scripts/backtrack/MODEL_NOTES.md`. Owner rules, binding:

  * training NEVER runs in CI or on AWS — it is owner-invoked, local, and
    on-demand (`train_export.py`, unchanged);
  * the scheduled workflow is PREDICT-ONLY: artifact + today's Supabase
    snapshot rows -> Picks-{ENV};
  * the inference path is the SAME code the Lambda handler ran, lifted
    verbatim into `run_daily_picks.py` — nothing was reimplemented.

## 1. Daily flow

The workflow `.github/workflows/ml-picks.yml` runs on a GitHub-hosted
runner:

1. **Scheduled:** `cron '30 9 * * *'` UTC daily (= 05:30 ET in DST, 04:30
   EST — the same fixed-UTC anchor the retired EventBridge
   `MLPicksDailyTrigger` used; GitHub cron does not track DST, so the local
   wall-clock time shifts one hour across clock changes. Accepted, as it
   was then).
2. **Manual:** `Actions -> ML Daily Picks -> Run workflow`, with optional
   inputs:
   * `date` — slate date `YYYY-MM-DD` (default: ET-today);
   * `dry` — smoke run: assembles + validates the 3 picks and prints the
     SMARTSCORE_ML_PICKS log line, but **skips the Picks write**.

Either way the job does: checkout -> `uv sync --frozen --no-dev` (the same
runtime dependency surface `deploy.yml`'s deploy job installs) -> artifact
fetch (default: Supabase Storage dated-prefix-latest; see section 3) ->
`run_daily_picks.py`, whose FIRST data step is the daily gap-fill
(`backfill_missing_days.backfill`, section 1.2 — the workflow also runs it
as its own step; the per-date markers make the second invocation a no-op
skip), then assembles the day's frame from THREE read-only sources
(docs/ml-picks-runbook.md §1.1 below):

* **today's rows** — the LIVE snapshot table `Player-Snapshots-{ENV}`
  (the daily snapshot the pipeline's snapshot build publishes each
  morning);
* **history** — the backtrack tables (Player-Snapshots / Team-Stats /
  Goalie-Snapshots `-backtrack-{ENV}`), STRICTLY before the pick date,
  fetched FULL-history (career_* are expanding career sums; a bounded
  window mis-states every veteran's block) via `model.fetch_table`'s
  month-window pagination;
* **the 14 shot/TOI features** for today's rows — recomputed EXACTLY by the
  SAME pandas core `model.shot_features()` runs
  (`shot_features_from_games`, imported, never forked) over the PUBLISHED
  per-game record: the stage-2 raw columns
  (`game_goals` / `game_shots` / `game_points` / `game_toi_min`,
  20261027_add_per_game_raw_columns_player_snapshots_backtrack.sql) turn
  every published backtrack row into one per-player-game line, and the
  career_* cells are exact expanding sums over the FULL fetched history.
  Published rows with no raw stat (pre-top-up lateness) are DROPPED from
  the ledger — the recompute then runs over the available raw rows (a
  partial-prefix recompute, LOUD-warned; train/run parity degrades from
  exact equality to a tolerance until the top-up closes the gap).
  Supabase IS the live data plane — no local sqlite, no dumps, no NHL-API
  sweep on the runner; the older stage-1 "last published row" carry-forward
  attach and the runner-side store refresh are both RETIRED from the
  primary path (the carry-forward survives ONLY as
  `run_daily_picks.carry_forward_shot_block`, the clearly-marked last-resort
  fallback when the exact recompute itself raises). The only inputs not on
  the player table are the team/goalie rates, read at publish cadence from
  Team-Stats / Goalie-Snapshots-backtrack (a lagging one prints a staleness
  warning, never an exit).

### 1.2 The daily gap-fill (first task of the day; run_daily_picks step 1 + ml-picks.yml first step)

The completed game days the nightly publish has not covered yet are topped
up BEFORE any history is read — typically exactly ONE day (yesterday),
0-1 on a normal day:

1. Reads the table's latest published date (one query).
2. For every completed-game date after it — usually exactly ONE day
   (yesterday) — it makes ONE schedule request per missing day
   (`nhl_client.fetch_schedule`) plus ONE boxscore request per game the
   schedule reports FINAL/OFF; games in any other state (LIVE/FUT) are
   SKIPPED — the FINAL-only guard keeps an in-flight box score out of
   nhl_client's HTTP cache (the cache-hostage bug) — and replayed on a
   later pass once final. Typical run: 2-5 NHL API requests total; a
   longer gap is capped at 14 days (`MAX_MISSING_DAYS`): anything larger
   needs the owner-side dump/local-store top-up (`publish_shot_cols.py`),
   never a runner sweep.
3. It upserts ONLY the per-game raw columns on (date, player_id)
   merge-duplicates, in two lanes so no published column is ever clobbered:
   a row the nightly publish already wrote gets ONLY the raw block
   (`game_shots`/`game_goals`/`game_points`/`game_toi_min`) — the 16
   precomputed rolling columns are untouched; a row that is missing is
   CREATED as a minimal snapshot shape (identity columns: date, player_id,
   season, name, team_abbrev, opponent_abbrev, scored + the raw block),
   with the rolling cells left NULL for the nightly publish to fill
   forward. It does NOT recompute the 16 rolling columns: those stay the
   training-side history the publish's attach carries.
4. It writes a per-date MARKER row (`Player-Game-Raw-Markers-{ENV}`, same
   migration) AFTER the date's upserts land. A date whose games were not
   all final, or whose writes failed, stays UNMARKED and is retried by a
   later pass; a MARKED date costs every later run NOTHING — no schedule
   request, no writes — so the first run of the day does the work once and
   every same-day re-run (the per-game kick fires ~15 runs per slate date;
   `run_daily_picks.run` re-invokes the helper each time) is a pure skip.
   Re-runs are idempotent by construction: 0 writes.
5. Every boxscore fetch is a FRESH GET: the helper deletes any existing
   `data/cache/boxscore-<id>.json` before fetching, so a boxscore file
   cached while the game was still LIVE (an earlier crawl that raced the
   final horn) can never be served as if final (nhl_client itself is
   unmodified; the override lives in the helper).

`dry=true` (the workflow's smoke input) makes the whole pass read-only: the
gap-fill is SKIPPED entirely (no upserts, no marker rows, no cache
unlinks) and `run_daily_picks` only LISTS the missing game days (one
Supabase read, one warning line).

Ordering note: the step needs the stage-2 DDL. `run_daily_picks` fails
LOUDLY naming the migration file when the columns are missing — apply
`20261027_add_per_game_raw_columns_player_snapshots_backtrack.sql`
(deploy.yml's migrate job does it automatically on merge to main; label a
`deploy` PR to apply it to dev first) and run the one-shot history top-up
BEFORE the first post-merge picks run.

### 1.2.1 Getting the 20261027 stage-2 DDL onto PROD — the two owner paths

Diagnosis (why prod is not migrated as of 2026-10-30): the `migrate` job
(deploy.yml:155-223) applies ONE env per run — `ENV=prod` ONLY on a push to
main (deploy.yml:162,166), `ENV=dev` on a `pull_request` carrying the
`deploy` label (deploy.yml:163). The migration exists only on
`feat/final-clean`/PR #146 (commit `ca1f44e`); main's latest scaffold
(`c002cc1`, run 40575778111) does not contain it. So the Oct-28 green
migrate run 40690521087 applied the file with `ENV=dev` (job log: "Applying
supabase/migrations/20261027_... .sql (ENV=dev)") — the `-dev` tables got
the four columns + `Player-Game-Raw-Markers-dev`; `-prod` was never
touched. The job succeeded, which also proves the repo secret
`SUPABASE_DB_URL` (deploy.yml:167) IS configured in Settings -> Secrets —
no missing secrets, no token needed (`SUPABASE_ACCESS_TOKEN` only matters
for the optional `supabase db push` variant, deploy.yml:200-209). The DML
top-up (`topup_raw_game_cols.py`) runs over PostgREST with the
service-role key (its client performs NO DDL, docs/backtrack_dev_deleted.md:47
is why DDL needs psql), so it does not depend on `SUPABASE_DB_URL`.

**PATH A — merge-then-auto-apply (zero manual SQL).**
1. Merge PR #146. The main-push `migrate` run re-applies every file
   idempotently with `ENV=prod`, so the four ALTERs + the marker CREATE
   land on `Player-Snapshots-backtrack-prod` with no owner action.
2. The next picks pass tops up YESTERDAY's (and up to
   `MAX_MISSING_DAYS = 14` calendar days of unmarked FINAL days,
   backfill_missing_days.py:95) rows automatically — first run writes,
   every same-day re-run skips on the marker rows.
3. History older than the 14-day window keeps `game_*` NULL until the
   one-shot top-up: `make topup-raw-game-cols ENV=prod --` (us-local,
   owner AWS/DB-side; needs data/raw_nhl.sqlite loaded) or a CI wrap —
   no make target existed, so `topup-raw-game-cols` was added to the
   Makefile; a CI job hint if wanted later:
   `gh workflow run`-style `Actions -> run topup_raw_game_cols
   --env prod` would need a new sparse job wiring the
   `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` secrets (the same ones the
   picks workflow already carries) — the local path is cheaper and needs
   no new YAML.

**PATH B — pre-merge manual apply (owner Supabase dashboard/psql).**
No env-var substitution in the dashboard: run ONE of the two verbatim
variants in the Supabase project's SQL editor / psql
(`psql "$DATABASE_URL" < ...`, the URL from the project's Database
settings — NOT the SUPABASE_SERVICE_ROLE_KEY: the service-role key is
PostgREST-only and CANNOT run DDL; see proof that PostgREST cannot
DROP/ALTER in docs/backtrack_dev_deleted.md:47):

DEV variant (only if re-scoping to dev):
```sql
ALTER TABLE "Player-Snapshots-backtrack-dev"
    ADD COLUMN IF NOT EXISTS "game_shots"   INTEGER,
    ADD COLUMN IF NOT EXISTS "game_goals"   INTEGER,
    ADD COLUMN IF NOT EXISTS "game_points"  INTEGER,
    ADD COLUMN IF NOT EXISTS "game_toi_min" DOUBLE PRECISION;
CREATE TABLE IF NOT EXISTS "Player-Game-Raw-Markers-dev" (
    "date" TEXT NOT NULL,
    "games" INTEGER NOT NULL,
    "player_games" INTEGER NOT NULL,
    "backfilled_at" TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT "Player-Game-Raw-Markers-dev_pkey" PRIMARY KEY ("date")
);
ALTER TABLE "Player-Game-Raw-Markers-dev" ENABLE ROW LEVEL SECURITY;
```

PROD variant (the one prod needs):
```sql
ALTER TABLE "Player-Snapshots-backtrack-prod"
    ADD COLUMN IF NOT EXISTS "game_shots"   INTEGER,
    ADD COLUMN IF NOT EXISTS "game_goals"   INTEGER,
    ADD COLUMN IF NOT EXISTS "game_points"  INTEGER,
    ADD COLUMN IF NOT EXISTS "game_toi_min" DOUBLE PRECISION;
CREATE TABLE IF NOT EXISTS "Player-Game-Raw-Markers-prod" (
    "date" TEXT NOT NULL,
    "games" INTEGER NOT NULL,
    "player_games" INTEGER NOT NULL,
    "backfilled_at" TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT "Player-Game-Raw-Markers-prod_pkey" PRIMARY KEY ("date")
);
ALTER TABLE "Player-Game-Raw-Markers-prod" ENABLE ROW LEVEL SECURITY;
```

Then complete the per-row fill (PostgREST/service-role WRITES are permitted
once the columns exist; the top-up is data-DML only):
`make topup-raw-game-cols ENV=prod` (equivalent:
`uv run python smartscore/scripts/backtrack/topup_raw_game_cols.py
--env prod`, idempotent PK upserts over the ~623k prod history rows; add
`SPOT_CHECK=1` to verify-and-exit). If instead PATH A was taken, this is
the same one-shot top-up — unchanged.

then builds the frame via `model.build_frame`/`add_derived(source="supabase")`
(a pick-date context row per fixture is injected from the live snapshot's
own morning rates and confirmed opponent goalie, since the backtrack tables
only gain a date's rows post-game), scores with
the ensemble's mean within-(date, tims) rank percentile, applies
`model.daily_picks` (one pick per tims group), validates the 3-pick shape,
and publishes through `save_picks_db` — the identical chain the Lambda
executed, worked through a schema-safe write. It emits ONE structured log line (`log: SMARTSCORE_ML_PICKS`,
JSON) and appends a headline to the job summary, plus a stale-artifact
warning when the artifact trains through a date older than 7 days.

### 1.1 Missing-day and empty-snapshot semantics (owner-approved)

* A missing backtrack day is **NORMAL**: the tables only gain a date's rows
  after that day's games end and the publish runs. The job's FIRST step
  (backfill_missing_days, section 1.2) tops up the completed game days
  after the latest published date directly from the NHL API, and the
  inference pass prints ONE warning (`WARNING: published history ends at
  <date>` / a "carry no per-game raw stat(s)" line) naming any day the
  recompute will miss, then proceeds — the affected features are computed
  over the published record it has. Never an exit.
* If the LIVE snapshot itself has no rows for the pick date (the snapshot
  build has not run yet), there is nothing to score: the run prints
  `no picks computed: no live snapshot rows for <date>`, appends the same
  line to the job summary, writes NO Picks rows, and exits 0 — CLEANLY, no
  traceback. The scheduled job stays green.
* The artifact staleness warning is unchanged (section 6).
* Train/run parity of the shot block is enforced by test
  `test_live_shot_block_matches_training_core` /
  `test_live_shot_block_replays_historical_date_exactly`
  (tests/unit/test_predict_picks.py): the live path's block equals
  `shot_features()`' training math for the same rows, max abs diff < 1e-9.

## 2. Artifact refresh (owner-run, local, on demand; NOT scheduled)

`train_export.py --upload` is STILL the owner's only training step:

    # a. refresh the raw store for the recent seasons (local only)
    make backtrack-store SEASON=2025   # runs local_store.py --build 2025
    #    (+ SEASON=2026 when the new season opens)
    # b. publish the newest rows to the backtrack tables (the history source:
    #    prior-games context for the ML pick pass - today's rows come from
    #    the LIVE Player-Snapshots table, and today's shot/TOI block from the
    #    raw store these dumps/rebuilds feed - see section 1.1)
    make backtrack-publish ENV=prod SEASON=2025 SEASON=2026
    # c. local on-demand train + export + upload
    make train-ml-export ENV=prod THROUGH=<latest> SEED=0
    #    (equivalently: uv run --with lightgbm==4.7.0 python \
    #     smartscore/scripts/backtrack/train_export.py --env prod \
    #     --source supabase --through-date <latest> --seed 0 --upload)

The **artifact-fact convention**: the newest artifact is always "whatever
the last `train_export.py --upload` pushed". The key is
`<prefix>/artifact_<train_through>_<seed>.joblib` under prefix `predict/ml`
(`ML_ARTIFACT_PREFIX` for S3, `ML_ARTIFACT_STORAGE_PREFIX` for Storage):
file names sort by vintage and `run_daily_picks.load_artifact` /
`load_artifact_storage` always pick the lexically latest `.joblib` under
the prefix — no config file to keep in sync, no workflow edit on a
refresh, and a rollback is a re-upload/keep of the older key (or trimming
the newer key away). The local file `train_export.py` writes,
`data/ml/artifact_<train_through>_<seed>.joblib`, and the upload key share
the same object — the workflow reads the UPLOADED key by default.

Latest vintage: `artifact_2026-10-29_0.joblib` (2026-10-30 owner refresh;
`--env prod --source supabase --seed 0` — 624,582 rows, frame_source
supabase, through 2026-10-29, seed 0, version 2) uploaded to Supabase
Storage `ml-artifacts/predict/ml/artifact_2026-10-29_0.joblib`
(byte-identical round-trip verified); the (2026-10-30 + 2026-10-29) local
dry smokes with it print NO staleness warning (`age=1d`) and 3 picks
(mean_max_prob 0.9815 / 0.9259). `artifact_2026-10-27_0.joblib` (and the
legacy `artifact_2026-06-14_0.joblib`) remain on the prefix as rollback
keys — the loader picks the lexically latest, so a rollback is a re-upload
of the older key.

**Artifact SOURCES, in strict resolution order** (`run_daily_picks
.pick_daily_bundle`):

1. `--artifact PATH` (a local file, e.g. committed under `data/ml/`) —
   always wins; the downstream inference is byte-identical across sources.
2. S3 bucket/prefix — only when `ML_S3_BUCKET` is set AND AWS credentials
   exist in the environment. If they are absent/denied and Storage is
   configured, the run FALLS THROUGH to Storage (loud log line).
3. **Supabase Storage** (`ML_STORAGE_BUCKET`, default bucket
   `ml-artifacts`, prefix `predict/ml`, PRIVATE bucket read with the
   service-role key) — the no-AWS path and the ml-picks workflow's
   DEFAULT: it uses the exact SUPABASE_* secrets the job needs anyway, so
   an artifact refresh = one owner-side upload command, zero new secrets.

Artifact **version 2** carries the logreg member as the FULL fitted train-time
preprocessing pipeline (SimpleImputer -> StandardScaler -> LogisticRegression);
version 1 artifacts auto-patch at load with a loud
"LEGACY ARTIFACT FORMAT" retrain warning (`train_export.py` below commit
"fix(artifact): export train-time preprocessing pipeline..." exported the bare
inner estimator, which crashed on NaN-bearing live frames).

## 3. Owner setup — CURRENT minimal-secrets checklist

The workflow's artifact DEFAULT is **Supabase Storage**, so the only
required secrets are the ones already needed:

1. **Add/confirm the Supabase secrets** (artifact read + inference writes):
   `SUPABASE_URL`, `SUPABASE_API_KEY`, `SUPABASE_SERVICE_ROLE_KEY`
   (repo `Settings -> Secrets and variables -> Actions`; the same secrets
   `deploy.yml` already uses — if they exist, nothing to do).
2. `MLPICKS_DISPATCH_TOKEN` (the per-game kick path's token) — if the
   kick architecture is used; the nightly cron path needs nothing extra.
3. *(OPTIONAL, S3 path only)* `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
   `ML_S3_BUCKET` (+ `AWS_REGION`) — only if the owner wants the artifact
   on S3. Absent secrets = the run reads Storage automatically.

Artifact refresh (section 2c) with NO AWS at all:

    make train-ml-export ENV=prod THROUGH=<latest> SEED=0  # S3 unchanged, needs AWS
    # NO-AWS equivalent (uploads to Supabase Storage bucket ml-artifacts):
    uv run --with lightgbm==4.7.0 python smartscore/scripts/backtrack/train_export.py \
        --env prod --source supabase --through-date <latest> --seed 0 \
        --upload --storage-bucket ml-artifacts

Verify the Storage upload, then trigger `dry: true` smoke (step 4). The
bucket auto-creates idempotently on first upload; it is PRIVATE — the
Actions runner downloads with the service-role key.

Other accepted artifact sources (no code change, same inference):
commit the artifact into the repo under `data/ml/` (it is a few MB
compressed joblib of weights — weights are not secrets) and pass
`--artifact data/ml/<artifact>.joblib`; or provide the 3 AWS secrets and
`ML_S3_BUCKET` for the legacy S3 read.

(The old runbook's ECR Public alias console step is DELETED — there is no
registry, no image, and no alias anymore.)

## 4. Expected accuracy by artifact vintage (measured, no tuning since)

Pooled out-of-fold pick accuracy (one pick per tims group/day). Source:
`smartscore/scripts/backtrack/MODEL_NOTES.md` (96f0ff2 cache rows) and the
same file's era table.

| vintage / era                                  | ensemble | LGBM    | hgpg    | gpg     |
|---|---:|---:|---:|---:|
| seed 0, pooled all-era (3,159 challenge dates) | **29.92%** | 29.060% | 28.047% | 26.496% |
| seed 1, pooled all-era                         | 29.41% | 29.376% | 28.047% | 26.496% |
| seed 3, pooled all-era                         | 29.82% | 28.997% | 28.047% | 26.496% |
| recommended bundle: seed 0                     | ~29.9% | — | — | — |

Since the stage-2 exact-feature design the INFERENCE-TIME rolling/career
block is computed exactly (same core, same per-game facts as training) —
the accuracy table above is governed by the ARTIFACT's vintage, not by the
attach: each artifact's `metadata.train_through` (printed on every run and
surfaced in the job summary) is the last date its training data covered,
and a vintage older than 7 days prints the section-6 staleness warning.
Refresh the artifact after landing data-plane changes so the model sees
the newest games.

Season-by-season (ensemble means, same model):

| season | ensemble | LGBM | hgpg | gpg |
|---|---:|---:|---:|---:|
| 2021-22 | 28.32% | 27.24% | 24.80% | 22.97% |
| 2022-23 | 30.28% | 28.48% | 30.18% | 27.33% |
| 2023-24 | 29.17% | 28.54% | 23.99% | 23.85% |
| 2024-25 | 29.41% | 29.86% | 30.36% | 29.32% |
| 2025-26 | 31.12% | 31.23% | 30.33% | 28.28% |

Strict 2023-24+ slice (667 dates): ensemble mean **29.868%** (29.735 /
29.835 / 30.035 for seeds 0/1/3), LGBM 29.835%, hgpg 28.136%, gpg 27.086%.
Owner-facing headline stays **~29.7% all challenge dates / ~29.9% strict
2023+** — none of the V16 follow-up experiments beat the +0.5pp-on-both-
seeds gate, and the only ≥30% (LGBM 30.36%) was leak-invalidated via
same-game `lineup_unit`.

## 5. Cost: $0.00

* GitHub Actions on a PUBLIC repo: free (standard hosted-runner minutes for
  public repos cost nothing; with the store refresh in the job this is
  ~tens of minutes a day across the cron pass and the kicks — still free).
* S3 artifact: free at this scale (a few MB per vintage, a couple of reads
  a day).
* No ECR, no Lambda, no EventBridge, no CloudWatch — the whole MLPicks AWS
  surface is gone.
* TOTAL ≈ **$0.00/month** (vs ~= $0.04-0.05/month for the retired image
  pipeline).

## 6. Roll-forward on a stale artifact

`run_daily_picks.py` carries the handler's stale-artifact guard unchanged:
if the artifact's `train_through` is **more than ARTIFACT_STALE_DAYS = 7
days** old, it prints
`WARNING: artifact trains through <date>, which is <N> days stale (refresh
with a local train_export.py --upload run)` and the job summary surfaces
the artifact vintage — the run still FAILS CLOSED if the frame is missing
today's rows, but it will happily score a stale artifact, so treat that as
the owner's reminder: the runbook's section 2a-2c is the roll-forward.
(For an emergency cut-over use the `date` input with yesterday's slate.)

## 7. What NO LONGER exists in the repo

* `smartscore/lambda/predict_picks/` entirely (Dockerfile, `handler.py`,
  the `ml_picks_rust_stub.py` image-build stub, `__init__.py`).
* `build_scripts/deploy_ml_lambda.sh` and the Makefile's `build-ml-image` /
  `deploy-ml-lambda` targets (plus `PUBLIC_ECR_ALIAS`/`PUBLIC_ECR_REPO`
  vars).
* `templates/template.yaml`'s MLPicks resources and parameters:
  `MLPicksFunction`, `MLPicksLogGroup`, `MLPicksDailyTrigger`,
  `MLPicksFunctionPermission`, `MLPicksArtifactReadPolicy`, the
  `MLS3Bucket`/`MLEcrImageUri` parameters, and the `MLS3Configured`/
  `MLImageConfigured` conditions.
* `deploy.yml`'s docker build/push + `--sync-only` sync steps and the
  `MLS3Bucket`/`MLEcrImageUri` parameter overrides.
* The `PUBLIC_ECR_ALIAS` secret and the ECR Public/private repositories and
  images in AWS.

Everything runs from a **GitHub-hosted runner** (`ubuntu-latest`) on the
schedule above. The `train-ml-export` Makefile target survives (it wraps
the kept `train_export.py`); the other 15 lambdas and the deploy flow are
untouched.

## 8. Open risks

* The LIVE snapshot must publish its context columns (REQUIRED_LIVE_COLUMNS
  in run_daily_picks.py — team_name / opp_goalie_* / the morning rates);
  the pass hard-fails (fail-closed) naming exactly what is missing
  (intentional), same as the old 16-column contract.
* The rolling/career shot block is EXACT as of the stage-2 design (the
  published per-game raw columns + the shared core): today's 14 shot/TOI
  features are recomputed from the same per-game facts the training frames
  were built from, with the daily gap-fill (section 1.2) closing yesterday's
  games before inference and per-date markers making re-runs free. The
  freshness chain is now: the gap-fill (0-1 missing days typical) -> the
  published record; a gap-fill failure degrades LOUDLY to
  exact-over-available-history (warn-not-exit, section 1.1) and the affected
  dates are retried by the next pass. What is still attached from PUBLISHED
  rows and therefore lags by the publish cadence (unchanged, by design): the
  team rates (`tgpg`, `otga`, `otshga`) from `Team-Stats-backtrack-{ENV}`
  and the goalie context (`gaa`/`save_pct`/...) from
  `Goalie-Snapshots-backtrack-{ENV}` plus the LIVE snapshot's own morning
  rates — those tables' grains (team totals, goalie starts) are not in the
  per-game raw columns' scope for the picks frame, and their freshness is
  governed by the same nightly publish they always had (section 2a-2b
  cadence, DAILY_DATA_RUNBOOK).
* `pick_rows_to_picks_db` whitelists Picks-{ENV} columns (minus
  REMOVE_KEYS — `ppg` is not a table column on either env and used to
  wipe-then-fail the write, PGRST204); a table schema change must be
  reflected in REMOVE_KEYS/the whitelist + migration. The write itself is
  fail-safe by order: `save_picks_db` schema-probes every payload key
  against the live table BEFORE deleting anything (unknown key = loud
  raise, table untouched), and on an insert failure after the wipe it
  re-inserts the captured pre-wipe rows (best-effort restore) and raises,
  so a failed run leaves the OLD table, never 0.
* DST drift: the fixed UTC cron moves an hour across clock changes
  relative to ET (section 1).
* Prod LIVE snapshot rows may be empty if the pipeline's snapshot build
  stops — the run then exits CLEANLY (warn + no picks + status 0, section
  1.1) every day until the build recovers; the daily green-but-empty
  summaries are the signal to check the pipeline.

## 9. Per-game kick (race-fixed: the kick fires from the state machine)

ADDITIVE to the nightly cron above: AWS EventBridge remains the SOURCE OF
TRUTH for "restart the pipeline ~5 minutes after every distinct game start";
the ML picks pass runs on the GitHub Actions runner, kicked from the END of
the pipeline run itself.

### 9.1 The new chain (Per-game kick)

* **Chain:** per-game `TriggerStateMachineAt_*-{ENV}` rule (created by
  `smartscore/utility.py::schedule_run`, game_start +
  `PIPELINE_RESTART_OFFSET_MIN` = 5 minutes) -> PlayerProcessingPipeline
  state machine RE-EXECUTION (a full refetch: GetTims re-reads the Tim's
  pick pool — started games leave it — and SaveToDb/PublishToDb rewrite the
  LIVE snapshot rows) -> new final `KickMLPicks` Task state invokes the
  `MLPicksKick-{ENV}` relay Lambda -> one `repository_dispatch`
  (`event_type: ml-picks-game`, `client_payload: {date, dry, context:
  'post-game-start'}`) into `ml-picks.yml`. ONE state-machine target per
  rule (the pre-#146 shape); the old parallel event relay target is gone.

  ```
  EventBridge @ game_start + 5m  ->  state machine restart (refetch full:
  GetTims -> SaveToDb -> [UpdateHistory] -> PublishToDb)  ->  KickMLPicks
  ->  repository_dispatch  ->  Actions ML pick pass (fresh groups)
  ```

* **Explicit-date relay semantics:** the relay no longer derives the slate
  date itself, and `schedule_run` no longer stamps a date into any target
  Input. The state machine passes the pick date its own writes used:
  `handle_publish_db` returns `{"date": event.date or get_date()}`, and the
  `KickMLPicks` state relays it as the explicit `{'date': <str>}` contract.
  The Toronto-today fallback inside `ml_picks_kick.py` remains ONLY for an
  owner manual invoke with no date at all (e.g. an ad-hoc console test of
  the dispatch plumbing).
* **GAME-START RACE GUARD (why this ordering):** the previous design fired
  the relay as a SECOND EventBridge target at game_start+5min — in parallel
  with the state machine restart. The Actions job could therefore run
  BEFORE the refetch completed and compute picks from stale groups (and
  stale tims: started games leave the pick pool). Now the kick is a state
  inside the same run, placed after PublishToDb: the ML pass starts the
  SECOND the write path completes, so its snapshot read is time-consistent
  with the rows just published. No wait/delay knob is needed anywhere —
  the ASL ordering itself is the serialize point (nothing serializes the
  ML-read after the writes anymore; now it is downstream of them).
* **The relay is bounded by design:** `smartscore/ml_picks_kick.py` uses
  stdlib urllib only, no ML dependencies, and never touches Supabase or
  Picks — it forwards `{date, dry}` and nothing more.
* **IAM:** `StateMachineExecutionRole` carries `lambda:InvokeFunction` on
  `MLPicksKickFunction` plus a `states.amazonaws.com` function-level
  permission (`MLPicksKickStatePermission`, SourceArn pinned to the
  pipeline state machine; **defensive** — the role grant is the primary
  source). The old `MLPicksKickFunctionPermission` (events.amazonaws.com,
  per-rule SourceArn) was removed with the parallel target.
* **PAT setup (one time):** create a **fine-grained PAT** scoped to this
  repository with **Actions: write** permission only, short expiry
  (30–90 days; note rotation below). Unchanged: store it as the GitHub
  repo secret `MLPICKS_DISPATCH_TOKEN` (Actions forbids secret names
  starting with `GITHUB_`; the Lambda env key stays
  `GITHUB_DISPATCH_TOKEN` — `deploy.yml` maps one to the other). deploy.sh
  then sets it as the CloudFormation `GITHUB_DISPATCH_TOKEN` parameter
  with `NoEcho: true`: either deploy with
  `GITHUB_DISPATCH_TOKEN=<pat> ./build_scripts/deploy.sh` (the script
  forwards it as a parameter via `CFN_PARAM_OVERRIDES`), or set it once
  through the console/CLI on the `SmartScore-{ENV}` stack — an
  `update-stack` without the parameter keeps the previously stored value.
  (Owner alternative if a CloudFormation-held PAT is not wanted: a
  classic repo-scoped token with `repo` scope + Actions write, stored the
  same way. NEVER commit the token; `deploy.sh` never prints it.)
* **Idempotency (multiple runs per day):** multiple state-machine runs per
  date are fine and EXPECTED (the morning/projections/lineup refresh rules
  all re-execute it, each firing exactly ONE kick at the end; the retained
  nightly cron pass remains the guaranteed full-day snapshot fallback).
  Every kick re-runs the same daily pass; the Picks write is a full
  wipe-then-reinsert of the run's 3 picks via `save_picks_db`
  (probe-before-delete + restore-on-insert-failure), so a repeat pass for
  the same date rewrites the same rows rather than duplicating them. The
  workflow serializes reruns via its `concurrency` group, so two kicks
  overlapping queue, not race.
* **Cost/rates for an NHL day:** ~15 games/day after the first game
  starts → ~15 kicks/day on the AWS side (each is one Lambda invocation
  inside the existing state machine run — no extra Lambda trigger) and ~15
  one-dispatch GitHub API calls (far below the repo's 60+ authenticated
  requests/hour headroom; no self-limiting needed). With the
  runner-side store refresh (section 1.2) each Actions pass costs ~10-20
  min (refresh) + ~3-6 min (the pass itself), so ~30 passes worst case per
  NHL day; on a public repo the hosted-runner minutes stay free, and the
  HTTP-cache step makes a same-day second pass cheap (newly FINAL games
  only).
* **Failed-kick semantics:** no `Retry` on `KickMLPicks` — a dispatch
  failure (e.g. expired PAT) fails the state machine run loudly and shows
  in CloudWatch, instead of silently dropping the pick pass. Re-triggering
  (a later game-start rule or a manual replay via the
  `workflow_dispatch` knob) is an idempotent re-kick.
* **Open risks:** PAT expiry silently breaks kicks before the nightly
  cron saves the day — the relay fails loudly (missing/invalid token →
  CloudWatch error per kick) and the workflow replay knob is
  `workflow_dispatch` with `date`; EventBridge fan-out is bounded (1
  target/rule, ~15 rules/day, delete_expired_rules recycles them
  nightly); duplicate kicks for the same game-start minute collapse into
  one rule (rule names are time-keyed); an owner manual Lambda invoke
  without a payload date falls back to Toronto-today.
* Actions minute limits: irrelevant for a public repo today, but if this
  repo goes private, free minutes are 2000/month — the job uses ~3-6
  minutes/day, introducing order-of-magnitude headroom before it matters.
