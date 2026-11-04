# Working on this repo

Order gate and warehouse push: a demo rebuild of a live stock-system-to-warehouse
integration, against local mocks and synthetic data.

## Hard rules
- Never call real APIs. Everything runs against local mocks.
- No real customer, staff, product or order data. Codenames only:
  client = "Client-Wine", warehouse = "Warehouse-3PL", second site = "Site-B",
  people = "Ops lead", "Finance lead". Systems are "Cin7-style stock system"
  and "Vision-style EDI".
- Secrets via environment variables only. `.env.example` is committed; `.env`
  never is. Never read or print secret values.
- Rules decide routing. AI never decides to send an order on its own.
- `.private/` is never committed.

## Architecture rules
1. Core logic never talks to external systems directly. It talks to
   interfaces. Mocks implement them now; real connectors can implement them
   later without changing core code.
2. All AI calls go through one LLM adapter (`src/chaain/llm/`), configured per
   client: provider, model and API key from config or environment.
3. Storage goes through a repository layer, so SQLite can be swapped for
   Postgres.
4. Client-specific values (rules, thresholds, locations, minimums) live in
   config files (`configs/<client>/`), not code.
5. Each workflow has its own folder under `src/chaain/workflows/`, reusing the
   connectors.

## Layout
```
src/chaain/
  core/            ledger (repository, SQLite, states), models, decisions, config
  llm/             provider interface: Anthropic, fake (tests), replay (demo)
  connectors/
    stock_system/  interface, mock, synthetic data generator
    warehouse/     interface, EDI XML builder, mock
    notifier/      interface, mocks
  workflows/
    order_push/    scheduler, poll, gate, push, notes reader and eval,
                   resolver, team queue and its CLI, demo day
configs/demo/      rules.yaml, settings.yaml (one folder per client profile)
evals/notes/       notes-reader eval cases and saved results
tests/
docs/              DESIGN, decisions, components, open-questions, production-path
```

## Commands
- `make setup`: create `.venv` with Python 3.12 and install pinned dependencies.
- `make test`: run all tests.
- `make demo`: a simulated day; `make queue AS="Ops lead"`: act on its queue.
- `make eval` / `make eval-score`: run or re-score the notes eval.
- Ask before adding a dependency. Pin exact versions in `requirements.txt`.
