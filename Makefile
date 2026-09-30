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
