#!/bin/bash
# One-time / refresh deploy of the ADDITIVE ML pick-pass resources:
#   MLPicks-{ENV} (image Lambda), its log group, 09:30 UTC trigger, and the
#   ML_S3_BUCKET / MLEcrImageUri parameters. Owner-run only; not scheduled.
# Requires: aws cli logged in; MLECR_IMAGE_URI set (from make build-ml-image);
# SUPABASE_URL / SUPABASE_API_KEY / SUPABASE_SERVICE_ROLE_KEY in the env the
# same way build_scripts/deploy.sh requires them (values are never echoed).
set -euo pipefail

ENV_VAL="${ENV:-prod}"
AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="SmartScore-${ENV_VAL}"

: "${MLECR_IMAGE_URI:?Run make build-ml-image first, or export MLECR_IMAGE_URI=acct.dkr.ecr.R.amazonaws.com/smartscore/predict-picks:latest}"
for VAR in SUPABASE_URL SUPABASE_API_KEY SUPABASE_SERVICE_ROLE_KEY; do
  [ -n "${!VAR:-}" ] || { echo "Error: $VAR is not set (needed by the stack params)"; exit 1; }
done

aws cloudformation deploy \
  --template-file ./templates/template.yaml \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    ENV="$ENV_VAL" \
    MLS3Bucket="${MLS3_BUCKET:-smartscore-ml-artifacts}" \
    MLEcrImageUri="$MLECR_IMAGE_URI" \
    SupabaseUrl="$SUPABASE_URL" \
    SupabaseApiKey="$SUPABASE_API_KEY" \
    SupabaseServiceRoleKey="$SUPABASE_SERVICE_ROLE_KEY"

echo "deployed: see docs/aws-predict-lambda-runbook.md step 3 for the smoke invoke"
