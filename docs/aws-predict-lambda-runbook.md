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

## 1. One-time setup: registry model + the ECR Public alias (once per account)

REGISTRY MODEL: the image's **distribution master lives on ECR Public**
(`public.ecr.aws/<alias>/smartscore/predict-picks`) — the 50 GB/month
always-free public tier — instead of piling every historical image into the
500 MB/month private cap. A Lambda function, however, can only pull its
container image from a **private ECR repo in the function's own region**
(Lambda docs, images-create: the repository "must be in the same AWS Region
as the Lambda function"; ECR Public is not a supported ImageUri source). So
the deploy step syncs the same image to a private repo that holds exactly
ONE image (keep-last-1 lifecycle policy), inside the private free tier.

The per-account public alias needs ONE console step — the CLI cannot create
it (`aws ecr-public` has no alias command; AWS docs: "a custom alias can be
set using the Amazon ECR console"):

1. AWS console -> Amazon ECR -> navigation pane **Registries** -> select your
   **Public** registry -> **Edit** -> **Custom alias** -> set the alias ->
   **Save changes**.
   * one custom alias per account, effectively permanent;
   * the request is human-reviewed — AWS approves "within a few days";
   * meanwhile (and ever after) the account's DEFAULT alias also works for
     pushes/pulls — find it with
     `aws ecr-public describe-registries --region us-east-1`
     (a random ~8-character string, not the account id).
2. Expose it to the tooling: `export PUBLIC_ECR_ALIAS=<alias>` locally, and
   set the `PUBLIC_ECR_ALIAS` repository secret for CI.

## 2. Build + push the image (on every handler/Dockerfile change; CI does it)

`export PUBLIC_ECR_ALIAS=<alias>; ENV=prod make build-ml-image`, which runs:

    docker build --platform linux/amd64 -f smartscore/lambda/predict_picks/Dockerfile -t smartscore/predict-picks:latest .
    # size sanity gate (job-failing): docker image inspect .Size vs
    # DOCKER_IMAGE_MAX_MB (default 5120 MB) - the always-free public tier
    # holds 50 GB/month, so 5 GB trips a runaway build long before money.
    aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws
    aws ecr-public describe-repositories --repository-names smartscore/predict-picks --region us-east-1 \
      || aws ecr-public create-repository --repository-name smartscore/predict-picks --region us-east-1
    docker tag smartscore/predict-picks:latest public.ecr.aws/$PUBLIC_ECR_ALIAS/smartscore/predict-picks:latest
    docker push public.ecr.aws/$PUBLIC_ECR_ALIAS/smartscore/predict-picks:latest

ECR Public notes (verified against AWS docs, Oct 2026):

  * the ecr-public API and login token are **us-east-1 only** — always
    authenticate to us-east-1 whatever the stack region; the Docker endpoint
    `public.ecr.aws` is global; tokens last 12 h;
  * pulls need NO AWS account (anonymous), throttled to 1 pull/second per
    source IP and 500 GB/month of anonymous transfer (authenticated:
    10 pulls/second, 5 TB/month free);
  * a stale cached token breaks pulls — fix with `docker logout public.ecr.aws`;
  * quotas: 10,000 public repositories per account (adjustable), max layer
    ~10,000 MiB, no untagged-image expiry on public repos (delete old digests
    yourself).

CI does all of this automatically in the deploy job (main push or
`deploy`-labelled PR): `make build-ml-image` (build + size sanity gate +
public master push), then `deploy_ml_lambda.sh --sync-only` (private copy +
exports MLECR_IMAGE_URI), then `deploy.sh` - whose stack update receives
MLS3Bucket/MLEcrImageUri and creates the ML resources in the SAME update as
the app. Fully CI-automated: a main push or `deploy`-labelled PR deploys
EVERYTHING (image build/push, private mirror, stack update) except the
one-time console alias, the initial S3 bucket creation (CI targets the
`ML_S3_BUCKET` secret or the `smartscore-ml-artifacts` default, created by
the first local artifact upload), and the artifact upload itself
(*training NEVER runs on AWS* — see section 4: `train_export.py --upload`
always ships the model from this machine). The owner only needs the alias +
`PUBLIC_ECR_ALIAS` secret set; with CI deploying, local docker is only
needed when the owner chooses to build/push manually.

## 3. Deploy the stack resources + smoke invoke

    ENV=prod MLS3_BUCKET=smartscore-ml-artifacts make deploy-ml-lambda

This calls `build_scripts/deploy_ml_lambda.sh`, which (a) syncs the locally
built image to the private repo `$ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/smartscore/predict-picks:latest`
with the keep-last-1 lifecycle policy, and (b) `aws cloudformation deploy`s
the additive resources onto the existing `SmartScore-$ENV` stack —
MLPicksFunction (MemorySize 2048, Timeout 300), its log group, the 09:30 UTC
trigger, and the artifact-read IAM. ALL stack parameters are passed
(CAPABILITY_NAMED_IAM; `cloudformation deploy` resets omitted parameters to
template defaults, which would wipe the Brevo/PostHog/Supabase wiring on the
shared stack). SUPABASE_URL / SUPABASE_API_KEY / SUPABASE_SERVICE_ROLE_KEY /
BREVO_* / FEATURE_SEND_EMAILS must be in env, same conventions as
`build_scripts/deploy.sh`; values are never echoed.

Smoke invoke (no Picks write):

    aws lambda invoke --function-name MLPicks-prod \
      --payload '{"date":"YYYY-MM-DD","dry":true}' out.json && cat out.json

Then a false-free check: `--payload '{"date":"YYYY-MM-DD"}'` writes the 3
picks via utility.save_to_db.

Verify the wiring: `MLEcrImageUri` -> MLPicksFunction (private URI); the
MLPicksDailyTrigger EventBridge rule `cron(30 9 * * ? *)` targets the
function; DST note in the template (fixed UTC anchor like the existing
cron(0 8 * * ? *)).

## 4. Manual artifact refresh (on demand; NOT scheduled)

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

## 5. Expected accuracy by artifact vintage (measured, no tuning since)

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

## 6. Free-tier budget (us-east-1, x86)

  * Lambda: 2048 MB x 300 s worst case ≈ 1024 GB-s per invocation — inside the
    monthly free 400k GB-seconds only if invocations stay rare. Expect the
    nightly frame build ~120-180 s wall-warm at 60-day window scope: ~260k
    GB-s/month for one daily run. Cold start adds the joblib import +
    lightgbm load (a few seconds at 2048 MB).
  * The image is free within the 50 GB/month ECR Public tier: the
    distribution master costs nothing at ~0.2 GB compressed (storage) plus
    anonymous pulls throttled at 500 GB/month transfer. The Lambda's pull
    copy lives in the PRIVATE repo bounded by keep-last-1 (one ~0.2 GB
    compressed image - ~40% of the 500 MB/month private free tier even
    before pruning; the CI size gate keeps a runaway build from ever
    landing; same-region Lambda pulls are free). Artifacts live in S3
    (~a few MB per vintage).
  * EventBridge rules, log ingest (1-day retention), and the S3 GetObject
    calls are all inside free allowance at this scale.
  * Free-tier numbers are the AWS documented values as of Oct 2026; verify
    against your account/region console before rollout — nothing here makes
    paid API calls.

## 7. Rollout ordering

1. Landing columns (16 cols migration/backfill complete for dev AND prod) —
   `docs/supabase_shot_columns.md`; PostgREST hard-fails a publish batch that
   names an unknown column, so migrate first.
2. Deploy the additive stack resources (image present before the deploy).
3. First real-artifact train + --upload, then a dry smoke invoke, then a
   real-day cut-over.
4. Keep the nightly pipeline (MakePredictions) running untouched; cutover of
   MakePredictions to the ensemble is a separate decision/PR.

## 8. Alarms note

Add (out of scope of this PR): a CloudWatch alarm on
`/aws/lambda/MLPicks-${ENV}`.Errors metric ≥ 1 in 5 min and a missing-invocation
alarm on the MLPicksDailyTrigger scheduled target (the pass is silent on a
quiet slate). RetentionInDays=1 keeps costs inside free tier.

## 9. DANGER before first deploy: prod snapshot rows may be empty

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

## 10. Open risks

  * The 16 columns are backfilled on dev and populated-on-prod by a sibling —
    if a new game day lands before its backfill completes, the ML pass
    hard-fails naming the missing columns (intentional fail-closed).
  * `pick_rows_to_picks_db` whitelists Picks-{ENV} columns; a table schema
    change must be reflected in the whitelist + migration.
  * Image size (ESTIMATE - docker is unavailable locally; measured by
    installing the Dockerfile's wheel set into a clean py3.12 venv, `du -sb`):
    runtime site-packages 265-295 MB (two installers resolved 309 MB and
    344 MB with --no-compile, minus ~45-53 MB of shipped
    tests/benchmarks/__pycache__ pruned in the builder stage) + the
    `public.ecr.aws/docker/library/python:3.12-slim` base (~125-130 MB
    unpacked) + ~0.2 MB of copied smartscore modules — call it ~390-430 MB
    unpacked, ~0.15-0.2 GB compressed. That is a ROUNDING ERROR against the
    real budget (ECR Public's always-free 50 GB/month storage), so the CI
    size gate is a 5120 MB SANITY FAIL, not a cost gate: it exists only to
    stop a runaway build (a fat base image, an accidental data COPY) long
    before it could matter (DOCKER_IMAGE_MAX_MB, default 5120, set in the
    Makefile). The slim base + pip-installed RIC (`awslambdaric`) shape was
    still chosen over the AWS Lambda runtime base for cold-start transfer
    size (that base alone measures 190.6 MB compressed from its public
    manifest, ~3x the whole slim-based image).
    Local-import verification of the full handler chain
    (ml_picks_handler -> model/config/utility/constants incl. the
    make_predictions_rust stub + smartscore-info-client) passed against
    the same pruned wheel tree.
  * `deploy.sh` (the app zip flow) passes MLS3Bucket/MLEcrImageUri ONLY when
    the caller exports them (CI does). A bare local `./deploy.sh` run omits
    them; update-stack resets omitted parameters to their template defaults
    (""), so the ML resources drop out of the stack until the next ML deploy
    (`make deploy-ml-lambda`).
  * The ECR Public alias request is human-reviewed ("within a few days");
    until it is approved, use the default alias from
    `aws ecr-public describe-registries --region us-east-1`.
