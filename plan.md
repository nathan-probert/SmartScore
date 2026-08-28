# Plan: CloudFormation change-set based stack update

Tracking issue: https://github.com/nathan-probert/SmartScore/issues/98

## Goal

Replace the fragile "grep the error string" no-op detection in `generate_smartscore_stack` (build_scripts/deploy.sh:39-54) with a clean change-set flow. Code-only deploys should detect "no changes" quickly and skip the CloudFormation wait, instead of calling `update-stack` and relying on `grep -q "No updates are to be performed."`.

## Scope

- Only the **update path** of `generate_smartscore_stack` in `build_scripts/deploy.sh`.
- No changes to `templates/template.yaml` or Lambda code.
- No changes to the create-stack path, step functions, or Lambda uploads (those are separate items in the issue).

## Approach

Replace the `describe-stacks` + `update-stack` + grep flow with:

1. `aws cloudformation create-change-set` with the same template and parameters as today, using a unique change-set name (e.g. `SmartScore-${ENV}-deploy`).
   - Keep `--capabilities CAPABILITY_NAMED_IAM`.
2. Poll for creation to finish:
   - Use `aws cloudformation wait change-set-create-complete` but treat a non-zero exit as expected when the change set is empty (the waiter fails when status == `FAILED`).
   - On failure, inspect `aws cloudformation describe-change-set` (Status / StatusReason) to classify the failure.
3. Classify the result:
   - **No changes:** change set status is `FAILED` and `StatusReason` contains `didn't contain changes`. Delete the change set (`delete-change-set`), print the same "No updates needed. Skipping wait." message, and continue.
   - **Real changes:** change set reaches `CREATE_COMPLETE`. Log it, `execute-change-set`, then `wait stack-update-complete` as today.
   - **Real failure:** any other `FAILED` reason (bad template, permissions, etc.). Print status and reason, abandon with exit 1.
4. Keep the existing post-update status assertion (deploy.sh:70-77) unchanged.

Note: after a successful `execute-change-set`, CloudFormation deletes the change set automatically; delete it manually only on the empty/no-changes and failure paths.

## Edge cases

- Empty change sets surface as `FAILED` + StatusReason "The submitted information didn't contain changes." — this is the expected, documented signal, not an error.
- The polling loop must exit on either `CREATE_COMPLETE` (proceed) or `FAILED` (classify), with a timeout safeguard.
- Change-set creation requires the exact same parameters as a real update; pass `ParameterKey=ENV`, `SupabaseUrl`, `SupabaseApiKey` verbatim.
- Avoid bare `set -e` around the waiter, since the empty-change-set wait exits non-zero by design.

## Verification

1. Run a deploy against `dev` with no template/parameter changes -> expect "No updates needed. Skipping wait." quickly, no CFN update initiated.
2. Make a trivial template change (or change a parameter) and deploy again -> expect change set to be `CREATE_COMPLETE`, execution + `stack-update-complete` wait runs.
3. Introduce a deliberate bad change set (e.g. invalid capability) -> expect script to fail loudly with status/reason, not silently continue.
4. Confirm the EventBridge role ARN SSM put-parameter step (deploy.sh:80-86) still runs after both paths.

## Out of scope (later items in issue #98)

- Parallelize + skip unchanged Lambda code uploads.
- Cache/combine the Docker compiler environments.
- Reuse CI `setup` job build artifacts in the deploy job.
- Zip size reduction.