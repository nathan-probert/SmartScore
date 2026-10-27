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
#
# The image is distributed from ECR PUBLIC (public.ecr.aws) - the 50 GB/month
# always-free public tier - NOT private-only. PUBLIC_ECR_ALIAS is the
# per-account public registry alias, a ONE-TIME console setup (Amazon ECR
# console -> Registries -> your Public registry -> Edit -> Custom alias; one
# alias per account, AWS reviews the request "within a few days"). Until it
# is approved, the DEFAULT alias works for pushes - find it with:
#   aws ecr-public describe-registries --region us-east-1
PUBLIC_ECR_ALIAS ?=
PUBLIC_ECR_REPO  ?= smartscore/predict-picks
MLS3_BUCKET      ?= $(if $(ML_S3_BUCKET),$(ML_S3_BUCKET),smartscore-ml-artifacts)
ML_ENV           ?= $(if $(ENV),$(ENV),prod)
ML_REGION        ?= us-east-1
# Size SANITY gate, not a budget: ECR Public's always-free allowance is
# 50 GB/month of storage, so this only trips a runaway build (a bad base
# image, an accidental training-data COPY) long before it costs money.
DOCKER_IMAGE_MAX_MB ?= 5120

# Local dry training + export + upload (the on-demand artifact refresh):
#   make train-ml-export ENV=prod SEASON=2023 THROUGH=2026-06-14 SEED=0
train-ml-export:
	ENV=$(ENV) uv run --with lightgbm==4.7.0 python $(BACKTRACK)/train_export.py \
	  --env $(ENV) $(if $(SEASON),--season $(SEASON),) $(if $(THROUGH),--through-date $(THROUGH),) \
	  --seed $(if $(SEED),$(SEED),0) --upload --s3-bucket $(MLS3_BUCKET)

# Build the predict-picks image and push it to ECR PUBLIC (one-time per code
# change; CI does this automatically on main-push/deploy-label deploys):
#   export PUBLIC_ECR_ALIAS=<alias>; make build-ml-image
build-ml-image:
	@test -n "$(PUBLIC_ECR_ALIAS)" || { \
	  echo "Error: PUBLIC_ECR_ALIAS is not set."; \
	  echo "The public registry alias is a ONE-TIME console step (CLI cannot create it):"; \
	  echo "  Amazon ECR console -> Registries -> <Public registry> -> Edit -> Custom alias."; \
	  echo "One alias per account; AWS reviews the request within a few days. The default"; \
	  echo "alias also works meanwhile: aws ecr-public describe-registries --region us-east-1"; \
	  echo "Then: export PUBLIC_ECR_ALIAS=<alias> and re-run."; \
	  echo "Full walkthrough: docs/aws-predict-lambda-runbook.md step 1."; \
	  exit 1; }
	docker build --platform linux/amd64 -f smartscore/lambda/predict_picks/Dockerfile -t $(PUBLIC_ECR_REPO):latest .
	@size=$$(docker image inspect -f '{{.Size}}' $(PUBLIC_ECR_REPO):latest); \
	  mb=$$(($$size / 1000000)); \
	  echo "Image size: $$mb MB (gate: DOCKER_IMAGE_MAX_MB=$(DOCKER_IMAGE_MAX_MB))"; \
	  [ "$$mb" -le "$(DOCKER_IMAGE_MAX_MB)" ] || { \
	    echo "Error: image is $$mb MB, over the $(DOCKER_IMAGE_MAX_MB) MB image-size gate."; \
	    echo "Local default is the 5120MB sanity bound (ECR Public always-free is 50 GB/month);"; \
	    echo "CI sets DOCKER_IMAGE_MAX_MB=500 as the HARD gate (ECR free-tier cap) via deploy.yml."; \
	    exit 1; }
	@# ECR Public API lives in us-east-1 only, regardless of the stack region.
	aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws
	aws ecr-public describe-repositories --repository-names $(PUBLIC_ECR_REPO) --region us-east-1 >/dev/null 2>&1 || \
	  aws ecr-public create-repository --repository-name $(PUBLIC_ECR_REPO) --region us-east-1 >/dev/null 2>&1 || true
	docker tag $(PUBLIC_ECR_REPO):latest public.ecr.aws/$(PUBLIC_ECR_ALIAS)/$(PUBLIC_ECR_REPO):latest
	docker push public.ecr.aws/$(PUBLIC_ECR_ALIAS)/$(PUBLIC_ECR_REPO):latest
	@echo "Pushed public.ecr.aws/$(PUBLIC_ECR_ALIAS)/$(PUBLIC_ECR_REPO):latest (distribution master; 50 GB/month free tier)"

# Create/refresh the MLPicks Lambda + trigger (additive; runs the same
# `cloudformation deploy` an owner would run manually after build-ml-image:
#   ENV=prod make deploy-ml-lambda
# The script syncs the built image to the PRIVATE repo for the function's
# ImageUri (Lambda can only pull private ECR in its own region) and passes
# the full stack parameter set.
deploy-ml-lambda:
	ENV=$(ML_ENV) MLS3_BUCKET=$(MLS3_BUCKET) \
	  bash build_scripts/deploy_ml_lambda.sh
.PHONY: train-ml-export build-ml-image deploy-ml-lambda
