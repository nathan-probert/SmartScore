local-setup:
	@echo Creating virtual environment
	@uv sync
	@$(MAKE) install

install:
	@echo Installing all dev dependencies
	@uv sync --all-groups

check-ci:
	@echo "Checking CI configuration"
	@$(MAKE) compile
	@$(MAKE) lint
	@$(MAKE) test

lint:
	@echo "Linting code"
	@uv run pre-commit run -a

test:
	@echo "Running tests with coverage"
	@uv run pytest -v --cov=smartscore --cov-report=term-missing --cov-report=html

test-no-cov:
	@echo "Running tests without coverage"
	@uv run pytest -v

integration:
	@echo "Running AWS-dev integration tests with mocked NHL data"
	@uv run pytest -v tests/integration

compile:
	@$(MAKE) compile_rust
	@$(MAKE) compile_c

compile_c:
	@echo "Compiling C code"
	@gcc -Wall -std=c99 -shared -o smartscore/compiled_code.so -fPIC smartscore/C/main.c

compile_rust:
	@echo "Compiling Rust code"
	@uv run maturin develop -r --manifest-path smartscore/Rust/make_predictions/Cargo.toml

get_odds:
	@echo "Getting odds"
	@ENV=prod uv run python smartscore/scripts/get_odds.py

watch_live:
	@echo "Running live"
	@uv run python smartscore/scripts/live_updates.py

# --- Local Floci development ---
local-up:
	@docker compose up -d
	@echo "Waiting for Floci..."
	@sleep 3
	@echo "Floci ready at http://localhost:4566"

local-down:
	@docker compose down

local-deploy:
	@LOCAL_MODE=1 ENV=local ./build_scripts/deploy.sh

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
