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
# SQLite and takes seconds. Both writes upsert (player rows on (date, player_id),
# team rows on (season, team_abbrev, game_id)), so re-running is safe.
backtrack-publish:
	@echo "Publishing $(SEASON) from the raw store into Supabase"
	@$(MAKE) backtrack-derive SEASON=$(SEASON)
	@ENV=$(ENV) uv run python $(BACKTRACK)/local_store.py --publish $(SEASON)
	@ENV=$(ENV) uv run python $(BACKTRACK)/local_store.py --publish-team $(SEASON)
