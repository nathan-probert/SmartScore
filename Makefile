.PHONY: local-setup
local-setup:
	@echo "Creating virtual environment"
	@$(MAKE) install

.PHONY: install
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
