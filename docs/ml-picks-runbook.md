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
fetch (default: S3 dated-prefix-latest; see section 3) ->
`run_daily_picks.py`, which loads the artifact, reads today's rows
read-only from the backtrack Supabase tables, hard-fails on any of the 16
migrated columns missing (docs/supabase_shot_columns.md), builds the frame
via `model.build_frame`/`add_derived(source="supabase")`, scores with the
ensemble's mean within-(date, tims) rank percentile, applies
`model.daily_picks` (one pick per tims group), validates the 3-pick shape,
and publishes through `save_picks_db` — the identical chain the Lambda
executed, worked through a schema-safe write. It emits ONE structured log line (`log: SMARTSCORE_ML_PICKS`,
JSON) and appends a headline to the job summary, plus a stale-artifact
warning when the artifact trains through a date older than 7 days.

## 2. Artifact refresh (owner-run, local, on demand; NOT scheduled)

`train_export.py --upload` is STILL the owner's only training step:

    # a. refresh the raw store for the recent seasons (local only)
    make backtrack-store SEASON=2025   # runs local_store.py --build 2025
    #    (+ SEASON=2026 when the new season opens)
    # b. publish the newest rows to the backtrack tables (this is what feeds
    #    the workflow's 16 shot/TOI+club snapshot columns day-of)
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
  public repos cost nothing; this job is a few minutes a day).
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

* The 16 columns must be backfilled/populated per env — the pass
  hard-fails (fail-closed) naming exactly what is missing (intentional),
  same as before.
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
* Prod snapshot rows may be empty if publishes stop — the pass fails
  closed until `make backtrack-publish ENV=prod SEASON=<year>` runs (the
  warning carries into the failing log line).

## 9. Per-game kick (A-architecture)

ADDITIVE to the nightly cron above: AWS EventBridge remains the SOURCE OF
TRUTH for "run ML picks ~5 minutes after every distinct game start"; the
compute runs on the GitHub Actions runner via a tiny relay Lambda.

* **Chain:** per-game `TriggerStateMachineAt_*-{ENV}` rule (created by
  `smartscore/utility.py::schedule_run`) -> second target
  `MLPicksKick-{ENV}` (target Id `mlPicksKickTarget`, fired at
  game_start + `ML_KICK_OFFSET_MIN` = 5 minutes, the knob at the top of
  `schedule_run` — change it in exactly one place) -> one
  `repository_dispatch` (`event_type: ml-picks-game`, `client_payload:
  {date, dry, context: 'post-game-start'}`) into `ml-picks.yml`. One EventBridge
  rule carries both targets (state machine + kick), consistent with
  EventBridge's multiple-targets-per-rule support and keeps the rule
  inside `delete_expired_rules()`' existing cleanup family.
* **The relay is bounded by design:** `smartscore/ml_picks_kick.py` uses
  stdlib urllib only, no ML dependencies, and never touches Supabase or
  Picks — it forwards `{date, dry}` and nothing more. The slate `date` is
  stamped into the target's Input at schedule time (America/Toronto — the
  pipeline's pick-date notion, cf. `service.get_date`), so a warm-lambda
  delay cannot shift the slate; the fallback is Toronto-today.
* **PAT setup (one time):** create a **fine-grained PAT** scoped to this
  repository with **Actions: write** permission only, short expiry
  (30–90 days; note rotation below). First store it as the GitHub repo
  secret `MLPICKS_DISPATCH_TOKEN` (Actions forbids secret names starting
  with `GITHUB_`; the Lambda env key stays `GITHUB_DISPATCH_TOKEN` —
  `deploy.yml` maps one to the other). deploy.sh then sets it as the
  CloudFormation `GITHUB_DISPATCH_TOKEN` parameter with `NoEcho: true`:
  either deploy
  with `GITHUB_DISPATCH_TOKEN=<pat> ./build_scripts/deploy.sh` (the
  script forwards it as a parameter via `CFN_PARAM_OVERRIDES`), or set it
  once through the console/CLI on the `SmartScore-{ENV}` stack — an
  `update-stack` without the parameter keeps the previously stored value.
  (Owner alternative if a CloudFormation-held PAT is not wanted: a
  classic repo-scoped token with `repo` scope + Actions write, stored the
  same way. NEVER commit the token; `deploy.sh` never prints it.)
* **Idempotent kick semantics:** multiple kicks per date are fine and
  EXPECTED (one per game start). Every kick re-runs the same daily pass;
  the Picks write is a full wipe-then-reinsert of the run's 3 picks via
  `save_picks_db` (probe-before-delete + restore-on-insert-failure), so a
  repeat pass for the same date rewrites the same rows rather than
  duplicating them. The workflow serializes reruns via
  its `concurrency` group, so two kicks overlapping queue, not race.
  its `concurrency` group, so two kicks overlapping queue, not race.
* **Cost/rates for an NHL day:** ~15 games/day after the first game
  starts → ~15 kicks/day on the AWS side (each is one Lambda invocation
  ~free) and ~15 one-dispatch GitHub API calls (far below the repo's 60+
  authenticated requests/hour headroom; no self-limiting needed). The
  Actions job itself costs ~3-6 minutes per pass, 30 passes worst case.
* **Nightly-cron interplay:** if both the 09:30 UTC cron and per-game
  kicks fire, the result is harmlessly ONE EXTRA pass — same upserts,
  logged in job history as two runs for that date. Leave both on; the
  cron remains the guaranteed full-day fallback.
* **Open risks:** PAT expiry silently breaks kicks before the nightly
  cron saves the day — the relay fails loudly (missing/invalid token →
  CloudWatch error per kick) and the workflow replay knob is
  `workflow_dispatch` with `date`; EventBridge rule fan-out is bounded
  (2 targets/rule, ~15 rules/day, delete_expired_rules recycles them
  nightly); duplicate kicks for the same game-start minute collapse into
  one rule (rule names are time-keyed).
* Actions minute limits: irrelevant for a public repo today, but if this
  repo goes private, free minutes are 2000/month — the job uses ~3-6
  minutes/day, introducing order-of-magnitude headroom before it matters.
