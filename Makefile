PYTHON := python3.12
VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup test clean read-note eval eval-score

setup: ## Create the virtual environment and install pinned dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt
	$(BIN)/pip install -e . --no-deps

test: ## Run all tests
	$(BIN)/pytest

read-note: ## Read one note with the real model, using your .env: make read-note NOTE="..."
	@set -a; [ -f .env ] && . ./.env; set +a; $(BIN)/python -m chaain.workflows.order_push.notes_reader "$(NOTE)"

eval: ## Run the notes eval with the real model, using your .env, and save the answers: make eval [RUNS=3] [MODEL=...]
	@set -a; [ -f .env ] && . ./.env; set +a; $(BIN)/python -m chaain.workflows.order_push.notes_eval run --runs $(or $(RUNS),3) $(if $(MODEL),--model $(MODEL))

eval-score: ## Score saved eval answers, no API call: make eval-score [FILE=evals/notes/results/...json]
	$(BIN)/python -m chaain.workflows.order_push.notes_eval score $(FILE)

clean: ## Remove the virtual environment and caches
	rm -rf $(VENV) .pytest_cache src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
