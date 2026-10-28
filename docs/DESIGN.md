# Order gate: design

## What this is
A small system that decides which wholesale orders can go from a stock system to
a third-party warehouse automatically, which need a person to check first, and
what to do when we can't tell whether the warehouse received an order.

It's a rebuild in code of a live integration I built in n8n for a wine importer.
This version runs against mock systems and synthetic data. It is structured so
the mocks can be replaced with real connectors.

## The problem
The importer's orders are created in a Cin7-style stock system and fulfilled by
a third-party warehouse through a Vision-style EDI API. The first version of the
integration, built in n8n, removed manual double entry. Running it in production
exposed three kinds of problem:

1. **Sending safely.** An order was delivered twice when the warehouse accepted
   it but our own log write failed, and the next run sent it again. Other issues
   came from a per-run cap applied before deduplication, and from retries after
   ambiguous responses.
2. **Judgement that lived in people's heads.** Credit holds, reserved stock,
   invoice-only sales, mixed pick locations and vintage changes were all checked
   by hand, or not at all.
3. **Uncertainty.** The warehouse only confirms receipt in its immediate
   response. When that response is missing or unclear, nothing in the system
   knows what happened.

## Design principles
- **Rules decide, AI assists.** Routing is deterministic. AI only reads free-text
  notes and turns them into structured flags with a confidence score. It never
  decides to send an order.
- **At most once, then ask.** Every order is claimed in the ledger before it's
  sent, and is never retried automatically. When the outcome is unclear, a
  person is asked, with the evidence.
- **Every decision has a reason.** Each state change is recorded with why it
  happened, so the team can trust and audit the system.
- **Built to be replaced in parts.** Core logic talks to interfaces, not to
  specific systems. Storage, connectors and the AI provider can each be swapped.

## How it works
1. **Poll** the stock system for invoiced orders changed within a lookback
   window. Deduplicate against the ledger before applying any per-run cap.
2. **Gate** each order with rules, plus flags from the notes reader:
   - SKIP: not for the warehouse (wrong channel, wrong site, invoice-only).
   - HOLD: needs a person (credit hold, mixed locations, below minimum,
     vintage or stock issue, reserved stock, new customer, uncertain notes).
   - AUTO: passes every check.
3. **Claim** the order in the ledger (PENDING) before sending. If the claim
   fails, don't send.
4. **Send** once. Accepted orders become SENT, rejected ones REJECTED, and
   anything unclear becomes CHECK.
5. **Resolve** CHECK and stale PENDING items on a separate schedule, by looking
   the order up where the warehouse supports it, or by asking the team.
6. **Review**: the team approves, rejects or confirms held and unclear items.
   Every action is logged with who did it and when.

## Order states
```
CANDIDATE -> SKIPPED
          -> HELD    -> (approved) PENDING
          -> PENDING -> SENT
                     -> REJECTED
                     -> CHECK -> RESOLVED (confirmed sent, or ready to retry)
```

## Where AI is and isn't used
| Step | Approach | Why |
| --- | --- | --- |
| Eligibility and exclusions | Rules | Clear, testable, no ambiguity |
| Credit, stock, minimums | Rules | Consequential and auditable |
| Free-text notes and instructions | AI, with confidence scores | Unstructured language; low confidence goes to a person |
| Whether to send | Rules plus people | Hard to reverse once the warehouse starts picking |
| Unclear delivery | People | Guessing risks a duplicate delivery |

The notes reader is measured against a hand-checked eval set, favouring recall
on risky flags: a wrongly held order costs a minute, a wrongly sent one costs a
delivery.

## Structure
```
src/chaain/
  core/          ledger, state machine, rules engine, review queue
  llm/           AI provider adapter (provider, model and key set per client)
  connectors/    stock system and warehouse interfaces, with mocks
  workflows/     order_push (further workflows can sit alongside)
configs/         client-specific rules and settings
evals/           eval sets for the AI step
tests/           one test per real incident or near-miss
docs/            decisions, components, open questions, production path
```

## Running it
See README.md. `make test`, `make eval` and `make demo`.

## Status
Demo: mock systems, synthetic data. See `docs/production-path.md` for what a
production version would need, and `docs/open-questions.md` for what's still
unknown about the real systems.

## How it was built
Designed and directed by me. Much of the code was written with Claude Code,
working from a written brief, with each design decision reviewed and recorded
in `docs/decisions.md`.
