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

