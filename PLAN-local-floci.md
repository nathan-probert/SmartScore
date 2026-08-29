# Plan: Local AWS Emulation with Floci

## Goal

Locally emulate all AWS services (Lambda, Step Functions, EventBridge, IAM, SSM, STS, CloudWatch Logs) using Floci (free, MIT-licensed, Docker-based emulator). Reuse the same CloudFormation template and ASL definitions for both AWS and local execution. AWS remains a fallback for production.

## Why Floci

- **MIT license** - truly free forever
- **83 AWS services** supported including all services SmartScore uses
- **Real Lambda containers** - uses actual AWS runtime images, supports Python 3.12
- **Full Step Functions** - ASL interpreter with nested `startExecution.sync`, Map, Retry/Catch
- **EventBridge scheduling** - PutRule, PutTargets, cron expressions
- **CloudFormation** - supports IAM, Lambda, Step Functions, Logs, SSM, EventBridge resource types
- **24ms startup, 13MB idle memory** - ideal for long-running server

## Compatibility Matrix

| SmartScore AWS Service | Floci Support | Notes |
|---|---|---|
| Lambda (Python 3.12, C/Rust `.so`) | Real Docker containers | Uses `public.ecr.aws/lambda/python3.12` |
| Step Functions (3 state machines) | Full ASL interpreter | Supports `startExecution.sync`, Map, Retry |
| EventBridge (cron rules + dynamic rules) | Full | PutRule, PutTargets, cron scheduling |
| IAM (3 roles) | Full | Users, roles, policies |
| SSM (parameter store) | Full | Parameter Store |
| STS (get caller identity) | Full | Returns account ID `000000000000` |
| CloudWatch Logs (11 log groups) | Full | Log groups, streams |

## Hard Requirements

- Docker must be running for Lambda execution (Floci uses real AWS runtime containers)
- Native extensions (C `.so`, Rust `.so`) must be compiled for `x86_64-unknown-linux-gnu` via existing Docker cross-compilation scripts
- CloudFormation template must work as-is with Floci's CloudFormation implementation. If any intrinsic function or resource type is unsupported, we fail and decide how to proceed.
- Supabase connects to existing hosted instance (no local database needed)
- Zip-upload approach for Lambda code (matches AWS deployment pattern)

## Files to Create/Modify

### 1. Create `docker-compose.yml`

```yaml
services:
  floci:
    image: floci/floci:latest
    ports:
      - "4566:4566"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./data/floci:/app/data
    environment:
      FLOCI_STORAGE_MODE: hybrid
      FLOCI_DEFAULT_REGION: us-east-1
      FLOCI_DEFAULT_ACCOUNT_ID: "000000000000"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:4566/_localstack/health"]
      interval: 5s
      timeout: 3s
      retries: 5
```

### 2. Create `.env.local`

```bash
# Floci / Local AWS
AWS_ENDPOINT_URL=http://localhost:4566
AWS_DEFAULT_REGION=us-east-1
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
AWS_ACCOUNT_ID=000000000000

# Environment
ENV=local

# Supabase (existing hosted instance)
SUPABASE_URL=<your-url>
SUPABASE_API_KEY=<your-key>
SUPABASE_SERVICE_ROLE_KEY=<your-role-key>

# Feature flags
FEATURE_SEND_EMAILS=false
POSTHOG_API_KEY=

# Brevo (optional)
BREVO_SMTP_LOGIN=
BREVO_SMTP_KEY=
BREVO_FROM_EMAIL=noreply@example.com
```

### 3. Modify `smartscore/utility.py`

Add `endpoint_url` support to all boto3 client factories. When `AWS_ENDPOINT_URL` is set, point boto3 at Floci.

Current pattern:
```python
_boto3_clients = {}


def get_lambda_client():
    if "lambda" not in _boto3_clients:
        _boto3_clients["lambda"] = boto3.client("lambda")
    return _boto3_clients["lambda"]
```

New pattern:
```python
import os


def _get_boto3_client(service_name):
    cache_key = service_name
    if cache_key not in _boto3_clients:
        kwargs = {}
        endpoint_url = os.environ.get("AWS_ENDPOINT_URL")
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        _boto3_clients[cache_key] = boto3.client(service_name, **kwargs)
    return _boto3_clients[cache_key]


def get_lambda_client():
    return _get_boto3_client("lambda")


def get_sts_client():
    return _get_boto3_client("sts")


def get_events_client():
    return _get_boto3_client("events")


def get_ssm_client():
    return _get_boto3_client("ssm")
```

The `invoke_lambda()` function already uses `get_lambda_client()` so no changes needed there.

### 4. Modify `build_scripts/deploy.sh`

Add `LOCAL_MODE` flag at the top of the script:

```bash
# --- at the top, after ENV/REGION defaults ---
LOCAL_MODE=${LOCAL_MODE:-0}
AWSCLI="aws"
if [ "$LOCAL_MODE" = "1" ]; then
  AWSCLI="aws --endpoint-url ${AWS_ENDPOINT_URL:-http://localhost:4566}"
fi
```

Then replace every `aws` call with `$AWSCLI` throughout the script. Key locations:
- Line 58: `if $AWSCLI cloudformation describe-stacks ...`
- Line 60: `$AWSCLI cloudformation update-stack ...`
- Line 78: `$AWSCLI cloudformation wait stack-update-complete ...`
- Line 82: `$AWSCLI cloudformation create-stack ...`
- Line 97: `$AWSCLI cloudformation wait stack-create-complete ...`
- Line 101: `$AWSCLI cloudformation describe-stacks ...`
- Line 111: `$AWSCLI cloudformation describe-stacks ...`
- Line 113: `$AWSCLI ssm put-parameter ...`
- Line 175: `$AWSCLI lambda update-function-code ...`
- Line 233: `$AWSCLI cloudformation describe-stacks ...`
- Line 244: `$AWSCLI stepfunctions describe-state-machine ...`
- Line 247: `$AWSCLI stepfunctions update-state-machine ...`
- Line 253: `$AWSCLI stepfunctions create-state-machine ...`

### 5. Modify `Makefile`

Add local development targets after the existing targets:

```makefile
# --- Local Floci development ---
local-up:
	@docker compose up -d
	@echo "Waiting for Floci..."
	@sleep 3
	@echo "Floci ready at http://localhost:4566"

local-down:
	@docker compose down

local-deploy:
	@LOCAL_MODE=1 ./build_scripts/deploy.sh

local-status:
	@AWS_ENDPOINT_URL=http://localhost:4566 aws cloudformation describe-stacks \
		--stack-name "SmartScore-local" \
		--query "Stacks[0].StackStatus" \
		--output text 2>/dev/null || echo "Stack not deployed"

local-stepfunctions:
	@AWS_ENDPOINT_URL=http://localhost:4566 aws stepfunctions start-execution \
		--state-machine-arn "arn:aws:states:us-east-1:000000000000:stateMachine:$(SM)-local" \
		--input '$(INPUT)'

local-invoke:
	@AWS_ENDPOINT_URL=http://localhost:4566 aws lambda invoke \
		--function-name "$(FUNC)-local" \
		--payload '$(PAYLOAD)' \
		--cli-binary-format raw-in-base64-out \
		response.json
	@cat response.json

local-logs:
	@AWS_ENDPOINT_URL=http://localhost:4566 aws logs tail \
		"/aws/lambda/$(FUNC)-local" --follow
```

### 6. Modify `.gitignore`

Add:
```
data/
```

## What Stays the Same

- `templates/template.yaml` - unchanged, deployed as-is via CloudFormation
- `templates/player_processing_pipeline.asl.json` - unchanged, `envsubst` patching works the same
- `templates/get_players.asl.json` - unchanged
- `templates/notify_users.asl.json` - unchanged
- Lambda handler code (`smartscore/event_handler.py`, `smartscore/service.py`) - unchanged
- Deploy script logic - only adds `LOCAL_MODE` flag
- ASL linting via `statelint` - unchanged
- Unit tests - unchanged (they mock boto3 already)

## Deployment Flow

```bash
# 1. Start Floci
make local-up

# 2. Deploy infrastructure + code
make local-deploy

# 3. Check status
make local-status

# 4. Trigger pipeline
make local-stepfunctions SM=PlayerProcessingPipeline INPUT='{"source":"test"}'

# 5. Tail logs
make local-logs FUNC=CheckCompleted

# 6. Stop
make local-down
```

## Known Limitations / Risks

1. **CloudFormation compatibility**: Floci supports the resource types SmartScore uses (IAM::Role, Lambda::Function, StepFunctions::StateMachine, Events::Rule, Logs::LogGroup), but if any intrinsic function or template construct is unsupported, deployment will fail with a clear error. We fix forward from there.

2. **Native extensions**: C and Rust `.so` files must be cross-compiled for `x86_64-unknown-linux-gnu` (Amazon Linux 2). Use existing `build_scripts/compile.sh` and `build_scripts/rust_compile.sh` which already handle this via Docker.

3. **Docker required**: Lambda functions execute in real AWS runtime containers. Docker Desktop must be running.

4. **Supabase**: Connects to hosted instance, not emulated locally. Network access to Supabase is required.

5. **EventBridge dynamic rules**: The `schedule_run()` function in `utility.py` creates EventBridge rules at runtime. These work in Floci but the scheduler dispatcher fires on a 10-second tick interval, not real-time.

## Follow-Up (Not in Scope)

- CI workflow integration (`.github/workflows/deploy.yml` with Floci)
- Hot-reload bind-mount setup for faster iteration
- Integration test harness against Floci
