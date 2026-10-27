#!/bin/bash
# One-time / refresh deploy of the ADDITIVE ML pick-pass resources:
#   MLPicks-{ENV} (image Lambda), its log group, 09:30 UTC trigger, and the
#   ML_S3_BUCKET / MLEcrImageUri parameters. Owner-run only; not scheduled.
#
# REGISTRY MODEL (docs/aws-predict-lambda-runbook.md step 1):
#   * the DISTRIBUTION MASTER is ECR PUBLIC (public.ecr.aws/<alias>/...,
#     50 GB/month always-free tier) - `make build-ml-image` pushes there;
#   * the function's ImageUri, however, MUST point at a PRIVATE ECR repo in
#     the stack's region: Lambda cannot pull public.ecr.aws images (AWS docs,
#     images-create; the repo must be "in the same AWS Region as the Lambda
#     function"). So this script syncs the same locally built image to the
#     private repo and points the stack at THAT copy, with a keep-last-1
#     lifecycle policy bounding private storage to ~one image (500 MB/month
#     private free tier).
#
# Requires: aws cli logged in; docker; the image built locally
# (`make build-ml-image` -> smartscore/predict-picks:latest); the SAME
# credential env vars build_scripts/deploy.sh requires (SUPABASE_* / BREVO_* /
# FEATURE_SEND_EMAILS; values are never echoed; POSTHOG_FEATURE_FLAG_KEY may
# be empty like deploy.sh's pass-through). Every stack parameter is passed
# explicitly: `cloudformation deploy` resets omitted parameters to their
# template defaults, which would wipe the Brevo/PostHog/Supabase wiring on
# the shared SmartScore-$ENV stack.
#
# `--sync-only`: just push the local image to the private Lambda-pull repo
# (create + lifecycle policy) and print/export MLECR_IMAGE_URI - no stack
# deploy, no Supabase credentials needed (CI uses this before deploy.sh).
set -euo pipefail

ENV_VAL="${ENV:-prod}"
AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="SmartScore-${ENV_VAL}"
REPO="${PUBLIC_ECR_REPO:-smartscore/predict-picks}"
SYNC_ONLY=0
if [ "${1:-}" = "--sync-only" ]; then SYNC_ONLY=1; fi

docker image inspect "$REPO:latest" >/dev/null 2>&1 || {
  echo "Error: local image $REPO:latest not found - run 'make build-ml-image' first."; exit 1; }

ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
[ -n "$ACCOUNT_ID" ] || {
  echo "Error: cannot resolve the AWS account id via 'aws sts get-caller-identity' (AWS_PROFILE=${AWS_PROFILE:-<unset>})." >&2
  echo "       Log in to the aws cli, or set AWS_ACCOUNT_ID / MLECR_IMAGE_URI explicitly." >&2
  exit 1; }
PRIVATE_URI="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO:latest"

# The private Lambda-pull copy: create if missing, then bound storage to the
# newest ONE image (any tag) so it never outgrows the 500 MB/month private
# free tier.
aws ecr describe-repositories --repository-names "$REPO" --region "$AWS_REGION" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "$REPO" --region "$AWS_REGION" >/dev/null
aws ecr put-lifecycle-policy --repository-name "$REPO" --region "$AWS_REGION" --lifecycle-policy-text \
  '{"rules":[{"rulePriority":1,"selection":{"tagStatus":"any","countType":"imageCountMoreThan","count":1},"action":{"type":"expire"}}]}' >/dev/null
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com" >/dev/null
docker tag "$REPO:latest" "$PRIVATE_URI"
docker push "$PRIVATE_URI"
if [ -n "${GITHUB_ENV:-}" ]; then echo "MLECR_IMAGE_URI=$PRIVATE_URI" >> "$GITHUB_ENV"; fi

if [ "$SYNC_ONLY" -eq 1 ]; then
  echo "--sync-only: synced $PRIVATE_URI (the deploy.sh stack update consumes it)"
  exit 0
fi

for VAR in SUPABASE_URL SUPABASE_API_KEY SUPABASE_SERVICE_ROLE_KEY \
           BREVO_SMTP_LOGIN BREVO_SMTP_KEY BREVO_FROM_EMAIL \
           FEATURE_SEND_EMAILS; do
  [ -n "${!VAR:-}" ] || { echo "Error: $VAR is not set (needed by the stack params; same conventions as build_scripts/deploy.sh)"; exit 1; }
done

# Ensure the artifact bucket exists (idempotent, fail-soft; us-east-1 is the
# special region: create-bucket must NOT get a LocationConstraint there).
# No extra bucket policy is required - the stack's MLPicksArtifactReadPolicy
# grants the Lambda role s3:GetObject on s3://<MLS3Bucket>/predict/ml/*
# (derived from the MLS3Bucket parameter, see templates/template.yaml).
MLS3_BUCKET="${MLS3_BUCKET:-smartscore-ml-artifacts}"
if ! aws s3api head-bucket --bucket "$MLS3_BUCKET" --region "$AWS_REGION" 2>/dev/null; then
  echo "S3 bucket '$MLS3_BUCKET' not accessible - creating it..."
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$MLS3_BUCKET" --region "$AWS_REGION" >/dev/null 2>&1 \
      || echo "Warn: could not create '$MLS3_BUCKET' (may exist elsewhere / permissions); the MLPicks artifact read would fail until it does." >&2
  else
    aws s3api create-bucket --bucket "$MLS3_BUCKET" --region "$AWS_REGION" --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null 2>&1 \
      || echo "Warn: could not create '$MLS3_BUCKET' (may exist elsewhere / permissions); the MLPicks artifact read would fail until it does." >&2
  fi
fi

aws cloudformation deploy \
  --template-file ./templates/template.yaml \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ENV="$ENV_VAL" \
    MLS3Bucket="${MLS3_BUCKET:-smartscore-ml-artifacts}" \
    MLEcrImageUri="$PRIVATE_URI" \
    SupabaseUrl="$SUPABASE_URL" \
    SupabaseApiKey="$SUPABASE_API_KEY" \
    SupabaseServiceRoleKey="$SUPABASE_SERVICE_ROLE_KEY" \
    BrevoSmtpLogin="$BREVO_SMTP_LOGIN" \
    BrevoSmtpKey="$BREVO_SMTP_KEY" \
    BrevoFromEmail="$BREVO_FROM_EMAIL" \
    FeatureSendEmails="$FEATURE_SEND_EMAILS" \
    PosthogApiKey="${POSTHOG_FEATURE_FLAG_KEY:-}"

echo "deployed: see docs/aws-predict-lambda-runbook.md step 3 for the smoke invoke"
