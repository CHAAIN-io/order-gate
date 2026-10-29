PYTHON := python3.12
VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup test clean

setup: ## Create the virtual environment and install pinned dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt
	$(BIN)/pip install -e . --no-deps

test: ## Run all tests
	$(BIN)/pytest

clean: ## Remove the virtual environment and caches
	rm -rf $(VENV) .pytest_cache src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
