.PHONY: local-setup
local-setup:
	@echo "Creating virtual environment"
	@$(MAKE) install

install:
	@echo "Installing dependencies"
	@poetry install --sync

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
	@echo "Compiling C code"
	@gcc -Wall -std=c99 -shared -o smartscore/compiled_code.so -fPIC smartscore/C/helper.c smartscore/C/main.c


.PHONY: get_odds
get_odds:
	@echo "Getting odds"
	@ENV=prod poetry run python smartscore/scripts/get_odds.py


.PHONY: watch_live
watch_live:
	@echo "Running live"
	@poetry run python smartscore/scripts/live_updates.py

