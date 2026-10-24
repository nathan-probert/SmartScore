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

# Backtrack: reconstructing season-to-date stats from NHL game logs.
#
# The raw store is the source of truth. It is not committed - data/dumps/*.sql.gz
# is (1.9 MB per season, reviewable as a diff) - so `backtrack-load` rebuilds the
# working database from those dumps. A fresh clone therefore needs no crawl to
# query the data.
#
#   make backtrack-load              rebuild data/raw_nhl.sqlite from the dumps
#   make backtrack-crawl SEASON=...  fetch a season from the NHL API
#   make backtrack-store SEASON=...  load the HTTP cache into the raw store
#   make backtrack-derive SEASON=... recompute the derived features
#   make backtrack-publish SEASON=... ENV=...  publish player and team rows
#   make backtrack-validate ENV=...  compare the local store against the archive
#   make backtrack-export            write the gzipped dumps that get committed
#   make backtrack-stats             summarise what the raw store holds
BACKTRACK := smartscore/scripts/backtrack

backtrack-load:
	@echo "Rebuilding the raw store from data/dumps/"
	@uv run python $(BACKTRACK)/dump_store.py --load

backtrack-crawl:
	@echo "Crawling $(SEASON) from the NHL API (slow: ~15 min per season)"
	@ENV=$(ENV) uv run python $(BACKTRACK)/reconstruct.py --season $(SEASON) --write

backtrack-store:
	@echo "Loading $(SEASON) into the raw store from the HTTP cache"
	@uv run python $(BACKTRACK)/local_store.py --build $(SEASON)

backtrack-derive:
	@echo "Recomputing derived features for $(SEASON) from the raw store"
	@uv run python $(BACKTRACK)/local_store.py --derive $(SEASON)

backtrack-validate:
	@echo "Comparing the local store against Player-Snapshots-$(ENV)"
	@ENV=$(ENV) uv run python $(BACKTRACK)/validate.py $(if $(ENV),--env $(ENV),) $(if $(SEASON),--season $(SEASON),)

backtrack-export:
	@echo "Writing gzipped dumps to data/dumps/ (these are what gets committed)"
	@uv run python $(BACKTRACK)/dump_store.py --export

backtrack-stats:
	@uv run python $(BACKTRACK)/dump_store.py --list
	@uv run python $(BACKTRACK)/local_store.py --stats

# Publish from the raw store - that is what makes it the source of truth.
# --derive runs first so the SQL, not a stale table, is what ships; it is pure
# SQLite and takes seconds. All three writes upsert (player rows on
# (date, player_id), team rows on (season, team_abbrev, game_id), goalie rows on
# (season, player_id, date)), so re-running is safe.
backtrack-publish:
	@echo "Publishing $(SEASON) from the raw store into Supabase"
	@$(MAKE) backtrack-derive SEASON=$(SEASON)
	@ENV=$(ENV) uv run python $(BACKTRACK)/local_store.py --publish $(SEASON)
	@ENV=$(ENV) uv run python $(BACKTRACK)/local_store.py --publish-team $(SEASON)
	@ENV=$(ENV) uv run python $(BACKTRACK)/local_store.py --publish-goalies $(SEASON)

# ---------------- Predict-only ML picks Lambda (ADDITIVE) ----------------
# Owner-run, on-demand only: training NEVER happens on AWS, and no target
# here is scheduled. The image ships feature inference + a Picks write; the
# artifact always comes from this machine via train_export.py.
#   docs: docs/aws-predict-lambda-runbook.md
MLS3_BUCKET     ?= $(if $(ML_S3_BUCKET),$(ML_S3_BUCKET),smartsscore-ml-artifacts)
MLECR_REPO      ?= smartsscore/predict-picks
ML_ENV          ?= prod
ML_REGION       ?= us-east-1

# Local dry training + export + upload (the on-demand artifact refresh):
#   make train-ml-export ENV=prod SEASON=2023 THROUGH=2026-06-14 SEED=0
train-ml-export:
	ENV=$(ENV) uv run --with lightgbm==4.7.0 python $(BACKTRACK)/train_export.py \
	  --env $(ENV) $(if $(SEASON),--season $(SEASON),) $(if $(THROUGH),--through-date $(THROUGH),) \
	  --seed $(if $(SEED),$(SEED),0) --upload --s3-bucket $(MLS3_BUCKET)

# Build the predict-picks image and push it to ECR (one-time per code change).
build-ml-image:
	docker build -f smartscore/lambda/predict_picks/Dockerfile -t $(MLECR_REPO):latest .
	$(if $(AWS_ACCOUNT_ID),aws ecr get-login-password --region $(ML_REGION) | docker login --username AWS --password-stdin $(AWS_ACCOUNT_ID).dkr.ecr.$(ML_REGION).amazonaws.com && aws ecr create-repository --repository-name $(MLECR_REPO) --region $(ML_REGION) 2>/dev/null; docker tag $(MLECR_REPO):latest $(AWS_ACCOUNT_ID).dkr.ecr.$(ML_REGION).amazonaws.com/$(MLECR_REPO):latest && docker push $(AWS_ACCOUNT_ID).dkr.ecr.$(ML_REGION).amazonaws.com/$(MLECR_REPO):latest,)

# Create/refresh the MLPicks Lambda + trigger (additive; runs the same
# `cloudformation deploy` an owner would run manually after build-ml-image:
#   ENV=prod MLECR_IMAGE_URI=<ecr/uri:tag> make deploy-ml-lambda
deploy-ml-lambda:
	ENV=$(ML_ENV) MLECR_IMAGE_URI=$(MLECR_IMAGE_URI) MLS3_BUCKET=$(MLS3_BUCKET) \
	  bash build_scripts/deploy_ml_lambda.sh
.PHONY: train-ml-export build-ml-image deploy-ml-lambda
