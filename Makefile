PYTHON := python3.12
VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup test clean read-note eval eval-score demo queue

setup: ## Create the virtual environment and install pinned dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt
	$(BIN)/pip install -e . --no-deps

test: ## Run all tests
	$(BIN)/pytest

read-note: ## Read one note with the real model, using your .env: make read-note NOTE="..."
	@set -a; [ -f .env ] && . ./.env; set +a; $(BIN)/python -m chaain.workflows.order_push.notes_reader "$(NOTE)"

eval: ## Run the notes eval with the real model, using your .env, and save the answers: make eval [RUNS=3] [MODEL=...] [PROMPT=notes-v1]
	@set -a; [ -f .env ] && . ./.env; set +a; $(BIN)/python -m chaain.workflows.order_push.notes_eval run --runs $(or $(RUNS),3) $(if $(MODEL),--model $(MODEL)) $(if $(PROMPT),--prompt $(PROMPT))

eval-score: ## Score saved eval answers, no API call: make eval-score [FILE=evals/notes/results/...json]
	$(BIN)/python -m chaain.workflows.order_push.notes_eval score $(FILE)

demo: ## Play a simulated day of about 20 orders, then show the team queue (offline)
	@$(BIN)/python -m chaain.workflows.order_push.demo

queue: ## Open the team queue on the demo day (offline): make queue AS="Ops lead"
	@$(BIN)/python -m chaain.workflows.order_push.queue_cli --as "$(AS)"

clean: ## Remove the virtual environment and caches
	rm -rf $(VENV) .pytest_cache src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
