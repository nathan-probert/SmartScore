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
`run_daily_picks.py`, which loads the artifact, then assembles the day's
frame from THREE read-only sources (docs/ml-picks-runbook.md §1.1 below):

* **today's rows** — the LIVE snapshot table `Player-Snapshots-{ENV}`
  (the daily snapshot the pipeline's snapshot build publishes each
  morning);
* **history** — the backtrack tables (Player-Snapshots / Team-Stats /
  Goalie-Snapshots `-backtrack-{ENV}`), STRICTLY before the pick date,
  fetched FULL-history (career_* are expanding career sums; a bounded
  window mis-states every veteran's block) via `model.fetch_table`'s
  month-window pagination;
* **the 14 shot/TOI features** for today's rows — computed EXACTLY — the
  SAME pandas core `model.shot_features()` runs
  (`shot_features_from_games`, imported, never forked) over the raw
  per-game store `data/raw_nhl.sqlite` + one synthetic today-row per
  player. The store is rebuilt ON THE RUNNER before inference by the job's
  refresh steps (section 1.2): the committed `data/dumps/<season>.sql.gz`
  dumps (`dump_store.load`) plus the current season's played games, crawled
  on HTTP-cache miss through the public NHL endpoints (`refresh_store.py`
  -> `local_store.build`/`nhl_client`). No Supabase anywhere in the
  refresh; no credentials in it at all.

### 1.2 The runner-side store refresh (ml-picks.yml, before inference)

The job's `picks` job runs, before the picks pass:

1. `dump_store.py --load` — un-gzips the committed season dumps into the
   runner's `data/raw_nhl.sqlite` (~6 min, measured 2026-10-27: 685,914
   player-games for 13 dumps). The dumps hold COMPLETED seasons; the
   current season's rows are not in any dump until the owner's
   `make backtrack-export` + commit, which is why step 2 exists.
2. `refresh_store.py --skip-load --through <ET slate date>` — `--skip-load`
   because step 1 already loaded the dumps (a pass pays the ~6 min dump
   load once; refresh_store falls back to a full load if the store is
   missing anyway). It drops the season
   schedule cache (never trusts a warm schedule file: DAILY_DATA_RUNBOOK §2
   gotcha), then `local_store.build(season)` walks the season schedule and
   fetches every played game the store lacks on HTTP-cache miss (box score
   + shift charts per game, per-player game logs, tims/goalie feeds).
   ~8 min cold on a fresh season start, ~1-3 min warm-served (measured
   nightly cadence: 7.5-8.2 min on the owner box, DAILY_DATA_RUNBOOK).

Idempotency/nature of the cache: `data/cache` (nhl_client's HTTP cache) is
kept between runs by an `actions/cache` step, so the second pass of a day —
the per-game kicks — re-fetches only newly FINAL games. Nothing else of the
refresh survives the job: `data/raw_nhl.sqlite` itself is rebuildable in
~that same time by the committed dumps, so the runner's per-run ephemeral
filesystem is fine. Steps run TWICE/day (nightly cron + kicks) without
re-downloading already-cached games; the crawl step is
`continue-on-error` — an NHL-endpoint flake DEGRADES the pass (the store
stays at the dumps' vintage; `run_daily_picks` prints its ONE stale-store
warning for the affected game days and continues, section 1.1), it never
kills the schedule. Say it plainly: **the refresh adds ~10-20 min per pass
and is cache-bounded**; on a public repo the runner minutes stay free.

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
  after that day's games end and the publish runs. The run prints ONE
  warning listing the uncovered game days inside the 60-day lookback
  (`WARNING: no history rows for game day(s) [...] ... continuing`) and
  proceeds — the affected prior-games features simply lag by those days.
  Never an exit.
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
* The shot block is only as fresh as the runner-side refresh chain (section
  1.2): a crawl failure leaves the store at the committed dumps' vintage and
  the pass degrades with the ONE stale-store warning (warn-not-exit,
  section 1.1). What is EXACT on the deployed path since
  cd05868 + the refresh commit: today's 14 shot/TOI features — the same
  data, the same core, dump-load + crawl rebuilt everywhere, parity < 1e-9
  (evidence: the refresh commit's parity note, 2026-10-23, 80 players).
  What is still attached from PUBLISHED rows and therefore lags by the
  publish cadence (unchanged, by design): the team rates (`tgpg`, `otga`,
  `otshga`) from `Team-Stats-backtrack-{ENV}` and the goalie context
  (`gaa`/`save_pct`/...) from `Goalie-Snapshots-backtrack-{ENV}` plus the
  LIVE snapshot's own morning rates — those tables' grains (team totals,
  goalie starts) are not in the raw per-game store's scope for the picks
  frame, and their freshness is governed by the same nightly publish they
  always had (section 2a-2b cadence, DAILY_DATA_RUNBOOK).
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
  authenticated requests/hour headroom; no self-limiting needed). With the
  runner-side store refresh (section 1.2) each Actions pass costs ~10-20
  min (refresh) + ~3-6 min (the pass itself), so ~30 passes worst case per
  NHL day; on a public repo the hosted-runner minutes stay free, and the
  HTTP-cache step makes a same-day second pass cheap (newly FINAL games
  only).
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
