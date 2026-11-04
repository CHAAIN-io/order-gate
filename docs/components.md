# Components

Each part of the system: what it does, whether it would be reused as-is for
another client or workflow, and how mature it is.

**Generic** means it holds no client-specific logic. **Client** means it
encodes this client's rules or systems; its values are in config, but its
logic is theirs.

**Maturity:**
- **Demo:** works for the demo; not built to run unattended.
- **Tested:** covered by unit and incident tests, against mocks.
- **Production:** running against real systems.

Nothing here is production yet. Paths are under `src/chaain/` unless they
start with `configs/` or `evals/`.

| Component | Path | Does | Scope | Maturity |
|---|---|---|---|---|
| Ledger | `core/ledger/` | One row per order, every state change with who and why; claim before send | Generic | Tested. SQLite; Postgres goes behind the same repository interface |
| State machine | `core/ledger/states.py` | The only moves allowed between the eight states | Generic | Tested |
| Models and decisions | `core/models.py`, `core/decision.py` | Sale, line, address; AUTO / HOLD / SKIP with reasons | Generic shape, stock-system fields | Tested |
| Config | `core/config.py`, `configs/demo/` | Loads per-client rules and settings | Generic loader, client values | Tested. Some values assumed (see open-questions.md) |
| LLM adapter | `llm/` | One interface for structured answers from a model; Anthropic, fake and replay providers | Generic | Tested with fakes. Live only via `make eval` |
| Notifier | `connectors/notifier/` | Sends an alert | Generic interface | Demo. Mocks only, no email leaves the machine |
| Stock system connector | `connectors/stock_system/` | Lists and fetches sales; mock plus a synthetic data generator with 28 named scenarios | Generic interface, Cin7-style data | Demo. No real API client |
| Warehouse connector | `connectors/warehouse/` | Builds the EDI XML and checks its limits; mock warehouse with scripted answers | Client: this warehouse's format | Tested against the spec as understood. The mock is demo |
| Scheduler | `workflows/order_push/scheduler.py` | Polling windows by time of day | Generic | Tested |
| Poll | `workflows/order_push/run.py` | Fetch, filter, dedup, cap, gate, push | Generic pattern | Tested, including the go-live incidents |
| Gate | `workflows/order_push/gate.py` | Skip and hold rules, each in hold / report / off mode | Client | Tested |
| Push | `workflows/order_push/push.py` | Claim, send once, record the outcome | Generic | Tested, including the October duplicate |
| Notes reader | `workflows/order_push/notes_reader.py`, `prompts/` | Free-text notes into flags with confidence | Generic mechanism, client prompt | Tested, plus the eval set |
| Notes eval | `workflows/order_push/notes_eval.py`, `evals/notes/` | Scores the reader on 26 notes, 3 runs each; suggests a threshold | Generic scorer, client cases | Tested. Results saved |
| Resolver | `workflows/order_push/resolver.py` | Finds stuck orders around the clock and alerts once | Generic | Tested |
| Team queue | `workflows/order_push/queue.py` | What needs a person, and the actions: approve, reject, received, retry | Generic | Tested |
| Queue command line | `workflows/order_push/queue_cli.py` | `make queue` | Demo stand-in for the real review interface | Demo |
| Demo day | `workflows/order_push/demo.py` | `make demo`: 21 scripted orders through both cut-offs | Demo | Demo, with a test that every order ends where expected |

## What another client or workflow would reuse

The ledger, state machine, LLM adapter, notifier, scheduler, poll, push,
resolver, queue and eval scorer, unchanged. A new client brings its own
config folder, gate rules, prompt and eval cases, and connectors for its own
systems. A new workflow gets its own folder under `workflows/`.
