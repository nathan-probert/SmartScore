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
#   make backtrack-export            write the gzipped dumps that get committed
#   make backtrack-stats            summarise what the raw store holds
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

backtrack-export:
	@echo "Writing gzipped dumps to data/dumps/ (these are what gets committed)"
	@uv run python $(BACKTRACK)/dump_store.py --export

backtrack-stats:
	@uv run python $(BACKTRACK)/dump_store.py --list
	@uv run python $(BACKTRACK)/local_store.py --stats

# Full rebuild from a fresh clone: load the dumps, then push the features into
# Supabase. Safe to re-run - the Supabase write upserts on (date, player_id).
backtrack-publish:
	@echo "Reconstructing into Player-Snapshots-backtrack-$(ENV) from the raw store"
	@ENV=$(ENV) uv run python $(BACKTRACK)/reconstruct.py --season $(SEASON) --write
