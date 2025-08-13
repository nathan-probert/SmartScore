
.PHONY: local-setup
local-setup:
	@echo Creating virtual environment and installing dependencies
	@uv venv .venv
	@uv pip install --system

install:
	@echo Installing all dev dependencies
	@uv pip install --system

.PHONY: check-ci
check-ci:
	@echo "Checking CI configuration"
	@$(MAKE) compile
	@$(MAKE) lint
	@$(MAKE) test


.PHONY: lint
lint:
	@echo "Linting code"
	@pre-commit run -a


.PHONY: test
test:
	@echo "Running tests"
	@pytest -v

.PHONY: compile
compile:
	@$(MAKE) compile_rust
	@$(MAKE) compile_c

.PHONY: compile_c
compile_c:
	@echo "Compiling C code"
	@gcc -Wall -std=c99 -shared -o smartscore/compiled_code.so -fPIC smartscore/C/main.c


.PHONY: compile_rust
compile_rust:
	@echo "Compiling Rust code"
	@maturin develop -r --manifest-path smartscore/Rust/make_predictions/Cargo.toml


.PHONY: get_odds
get_odds:
	@echo "Getting odds"
	@ENV=prod python smartscore/scripts/get_odds.py


.PHONY: watch_live
watch_live:
	@echo "Running live"
	@python smartscore/scripts/live_updates.py
