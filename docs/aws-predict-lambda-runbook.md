# AWS predict-only ML Lambda runbook (MLPicks-{ENV})

Authored against branch `feat/aws-relambda` at `c82b2e7` (+this branch's
Lambda commits) and verified against `templates/template.yaml`,
`smartscore/utility.py:69-92` + `smartscore/service.py:315` (publish_public_db
-> save_to_db), `build_scripts/deploy.sh` (JSON-env conventions), and
`smartscore/scripts/backtrack/MODEL_NOTES.md`. Owner rules, binding:

  * training NEVER runs on AWS. The Lambda is predict-only;
  * on-demand local training produces the versioned artifact on S3;
  * the Lambda = artifact + today's Supabase snapshot rows -> Picks-{ENV};
  * nothing runs locally on a daily schedule — training is owner-invoked.

## 1. One-time deploy

1. **Build+push the image ECR** (not scheduled; on every handler code change):
       export AWS_ACCOUNT_ID=<id>; ENV=prod make build-ml-image
   (builds `smartscore/lambda/predict_picks/Dockerfile`, python:3.12-slim +
   pandas/numpy/scipy/sklearn/lightgbm==4.7.0, pushes to
   `$ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/smartscore/predict-picks:latest`).
2. **Deploy the additive stack resources** (MLPicksFunction image Lambda,
   MemorySize 2048, Timeout 300, its log group, the 09:30 UTC trigger, and the
   artifact-read IAM too):
       ENV=prod MLECR_IMAGE_URI=$ACCT.dkr.ecr.us-east-1.amazonaws.com/smartscore/predict-picks:latest \
       MLS3_BUCKET=smartscore-ml-artifacts make deploy-ml-lambda
   This calls `build_scripts/deploy_ml_lambda.sh` -> `aws cloudformation deploy`
   on the existing `SmartScore-$ENV` stack (same conventions as
   `build_scripts/deploy.sh`; SUPABASE_URL / SUPABASE_API_KEY /
   SUPABASE_SERVICE_ROLE_KEY must be in env and are never echoed).
3. **Trigger** (verify): `MLEcrImageUri` wired to the MLPicksFunction; the
   MLPicksDailyTrigger EventBridge rule `cron(30 9 * * ? *)` targets the
   function; DST note in the template (fixed UTC anchor like the existing
   cron(0 8 * * ? *)).
4. **Smoke invoke** (no Picks write):
       aws lambda invoke --function-name MLPicks-prod \
         --payload '{"date":"YYYY-MM-DD","dry":true}' out.json && cat out.json
   Then a false-free check: `--payload '{"date":"YYYY-MM-DD"}'` writes the 3
   picks via utility.save_to_db.

## 2. Manual artifact refresh (on demand; NOT scheduled)

    # a. refresh the raw store for the recent seasons (local only)
    make backtrack-store SEASON=2025   # runs local_store.py --build 2025
    #    (+ SEASON=2026 when the new season opens)
    # b. publish the newest rows to the backtrack tables (this is what feeds
    #    the Lambda's 16 shot/TOI+club snapshot columns day-of)
    make backtrack-publish ENV=prod SEASON=2025 SEASON=2026
    # c. local on-demand train + export + upload
    uv run python smartscore/scripts/backtrack/train_export.py \
      --env prod --source supabase --through-date <latest> --seed 0 --upload
    #      (default is no upload — --upload is explicit)

The S3 key is `<prefix>/artifact_<train_through>_<seed>.joblib`; file names
sort by vintage, and the Lambda always picks the lexically latest .joblib.

## 3. Expected accuracy by artifact vintage (measured, no tuning since)

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

Strict 2023-24+ slice (667 dates): ensemble mean **29.868%** (29.735 / 29.835 /
30.035 for seeds 0/1/3), LGBM 29.835%, hgpg 28.136%, gpg 27.086%. Owner-facing
headline stays **~29.7% all challenge dates / ~29.9% strict 2023+** — none of
the V16 follow-up experiments beat the +0.5pp-on-both-seeds gate, and the
only ≥30% (LGBM 30.36%) was leak-invalidated via same-game `lineup_unit`.

## 4. Free-tier budget (us-east-1, x86)

  * Lambda: 2048 MB x 300 s worst case ≈ 1024 GB-s per invocation — inside the
    monthly free 400k GB-seconds only if invocations stay rare. Expect the
    nightly frame build ~120-180 s wall-warm at 60-day window scope: ~260k
    GB-s/month for one daily run. Cold start adds the joblib import +
    lightgbm load (a few seconds at 2048 MB).
  * The image lives in ECR (500 MB free monthly storage classband with the
    first pull allowed); artifacts live in S3 (~a few MB per vintage).
  * EventBridge rules, log ingest (1-day retention), and the S3 GetObject
    calls are all inside free allowance at this scale.
  * Grátis-tier numbers are the AWS documented values as of Oct 2026; verify
    against your account/region console before rollout — nothing here makes
    paid API calls.

## 5. Rollout ordering

1. Landing columns (16 cols migration/backfill complete for dev AND prod) —
   `docs/supabase_shot_columns.md`; PostgREST hard-fails a publish batch that
   names an unknown column, so migrate first.
2. Deploy the additive stack resources (image present before the deploy).
3. First real-artifact train + --upload, then a dry smoke invoke, then a
   real-day cut-over.
4. Keep the nightly pipeline (MakePredictions) running untouched; cutover of
   MakePredictions to the ensemble is a separate decision/PR.

## 6. Alarms note

Add (out of scope of this PR): a CloudWatch alarm on
`/aws/lambda/MLPicks-${ENV}`.Errors metric ≥ 1 in 5 min and a missing-invocation
alarm on the MLPicksDailyTrigger scheduled target (the pass is silent on a
quiet slate). RetentionInDays=1 keeps costs inside free tier.

## 7. DANGER before first deploy: prod snapshot rows may be empty

`Player-Snapshots-backtrack-prod` was recently observed EMPTY (the publish
phases have not been run against prod recently). The Lambda hard-fails
(fail-closed) the moment today's rows are missing or lack the 16 shot/TOI +
club columns. Before ANY deploy or live invocation:

  * run `make backtrack-publish ENV=prod SEASON=<year>` at least once and
  * verify with any GBQ-free read, e.g. a tiny
    `python -c` probe of `Player-Snapshots-backtrack-prod` for yesterday's
    date, that rows exist AND carry sd_shots_pg / r10_toi_pg / team_abbrev /
    opponent_abbrev etc. (all 16; see docs/supabase_shot_columns.md).
  * the nightly scheduled MakePredictions pass keeps writing picks
    independently; this ML pass writes only its own rows to Picks-{ENV}.

## 8. Open risks

  * The 16 columns are backfilled on dev and populated-on-prod by a sibling —
    if a new game day lands before its backfill completes, the ML pass
    hard-fails naming the missing columns (intentional fail-closed).
  * `pick_rows_to_picks_db` whitelists Picks-{ENV} columns; a table schema
    change must be reflected in the whitelist + migration.
  * Image size: measured locally, the pure-CDN wheel set (pandas/numpy/scipy/
    sklearn/lightgbm 4.7.0/boto3/supabase/joblib/pytz) unpacks to ~324 MB;
    with the python:3.12-slim base (~130 MB unpacked) and the ~15 MB of
    copied smartscore modules the image is ~490-540 MB unpacked (~190 MB
    compressed) - well under Lambda's 10 GB image limit. First-pull cold
    starts pay the transfer once per region.
