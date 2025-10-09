.PHONY: local-setup
local-setup:
	@echo Creating virtual environment
	@poetry env activate
	@$(MAKE) install

install:
	@echo Installing all dev dependencies
	@poetry install --with dev

.PHONY: check-ci
check-ci:
	@echo "Checking CI configuration"
	@$(MAKE) compile
	@$(MAKE) lint
	@$(MAKE) test

.PHONY: lint
lint:
	@echo "Linting code"
	@poetry run pre-commit run -a

.PHONY: test
test:
	@echo "Running tests"
	@poetry run pytest -v

.PHONY: compile
compile:
	@$(MAKE) compile_rust
	@$(MAKE) compile_c

.PHONY: compile_c
compile_c:
	@echo "Compiling C code"
	@gcc -Wall -std=c99 -shared -o smartscore/compiled_code.so -fPIC smartscore/C/main.c

.PHONE: compile_rust
compile_rust:
	@echo "Compiling Rust code"
	@poetry run maturin develop -r --manifest-path smartscore/Rust/make_predictions/Cargo.toml
	@poetry run maturin develop -r --manifest-path smartscore/Rust/test_weights/Cargo.toml

.PHONY: get_odds
get_odds:
	@echo "Getting odds"
	@ENV=prod poetry run python smartscore/scripts/get_odds.py

.PHONY: watch_live
watch_live:
	@echo "Running live"
	@poetry run python smartscore/scripts/live_updates.py
