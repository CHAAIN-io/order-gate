# Order gate: design

## What this is
A small system that decides which wholesale orders go from a stock system to
a third-party warehouse automatically, which need a person to check first,
and what to do when nobody can tell whether the warehouse received an order.

It is a rebuild in code of a live integration built in n8n for a wine
importer (Client-Wine). This version runs against mock systems and synthetic
data, and is structured so the mocks can be replaced with real connectors.

## The problem
Orders are created in a Cin7-style stock system and fulfilled by a
third-party warehouse (Warehouse-3PL) through a Vision-style EDI API. The n8n
integration, live since August 2026, removed double entry. Running it in
production exposed three kinds of problem:

1. **Sending safely.** A duplicate delivery in October, a per-run cap that
   hid orders on go-live day, and retries after unclear responses. See the
   README for the full history.
2. **Judgement that lived in people's heads.** Credit holds, invoice-only
   sales, mixed pick locations, Shopify orders on warehouse stock, one-off
   delivery changes: all checked by hand, or not at all.
3. **Uncertainty.** The warehouse confirms receipt only in its immediate
   response. When that's missing or unclear, nothing in the system knows
   what happened, and the stale-order check only runs when new orders arrive.

## Design principles
- **Rules decide, AI assists.** Routing is deterministic. AI only reads
  free-text notes, and can only hold an order, never send one.
- **At most once, then ask.** Every order is claimed in the ledger before
  it's sent, and is never retried automatically. When the outcome is
  unclear, a person is asked, with the evidence.
- **Every decision has a reason.** Each state change records why, and who.
- **Built to be replaced in parts.** Core logic talks to interfaces. Storage,
  connectors and the AI provider can each be swapped.
- **Client values in config.** Rules and thresholds live in
  `configs/<client>/`; logic lives in code, with tests.

## How it works
1. **Poll** the stock system on a timetable: every 15 minutes from 08:00 to
   20:00, and every 5 from 11:00 to 13:30, the run-up to the cut-off. Take
   invoiced sales changed in the last 48 hours.
2. **Deduplicate** against the ledger, *then* apply the per-run cap of 10.
   Orders cleared for retry are fetched even if they've left the window.
3. **Gate** each order:
   - SKIP: not for the warehouse. Every line picked at Site-B, an internal
     customer, #NOVISION, credited, voided, or not invoiced yet.
   - HOLD: a person decides. Credit hold, stock shortfall, mixed locations,
     Shopify on warehouse stock, data the warehouse would refuse, a new
     customer, or a note the notes reader flags. Every reason is listed.
   - AUTO: passes every check. Vintage changes, reserved stock and minimums
     are reported alongside, not held, until a shadow run shows they should be.
4. **Claim** the order (PENDING). If the claim fails, don't send.
5. **Send** once. The answer decides the state; anything unclear is CHECK.
6. **Resolve**, separately, every 15 minutes around the clock: a claim with
   no outcome after 30 minutes becomes CHECK. Where the warehouse can be
   asked, "found" confirms it as SENT. Anything newly needing a person goes
   into one alert.
7. **Review** in the team queue: unknowns first, then holds, then refusals.
   Approve, reject, received or retry, each logged with a name. Approving a
   hold that has changed since puts it through the gate again first.

## Order states
```
(first seen) ─► CANDIDATE ─► SKIPPED          (final for #NOVISION,
                    │                          credited, voided)
                    ├──────► HELD ──approve──► PENDING
                    │          └──reject───► SKIPPED (final)
                    └─AUTO + claim──► PENDING
                                        ├─ accepted ───────► SENT (final)
                                        ├─ refused ────────► REJECTED ─fixed─► RETRY
                                        ├─ never left us ──► RETRY
                                        └─ unclear/stale ──► CHECK
                                                               ├─ it arrived ─► SENT
                                                               └─ it didn't ──► RETRY
RETRY and non-final SKIPPED go through the gate again.
```
There is no way back to PENDING from SENT or CHECK. That is what keeps it
at most once: nothing reaches the warehouse twice unless a person first says
it didn't arrive.

## Where AI is and isn't used
| Step | Approach | Why |
| --- | --- | --- |
| Eligibility and exclusions | Rules | Clear, testable |
| Credit, stock, locations, minimums | Rules | Consequential and auditable |
| Free-text notes | AI, with confidence; raised or unsure flags hold | Unstructured language |
| Whether to send | Rules, plus people for holds | Hard to reverse once picking starts |
| Unclear delivery | People | Guessing risks a duplicate delivery |

The notes reader is measured on 26 synthetic notes, 3 runs each, with
results saved and re-scored offline. notes-v2: no missed holds and no wrong
holds. Missing a risky note costs a delivery, and holding a safe one costs a
minute, so the threshold is chosen for no misses first. In production, AI
starts in report-only mode and holds only if the shadow run shows it's worth
it (decision 16).

## Structure
```
src/chaain/
  core/            ledger (repository, SQLite, states), models, decisions, config
  llm/             model provider interface: Anthropic, fake, replay
  connectors/
    stock_system/  interface, mock, synthetic data generator
    warehouse/     interface, EDI XML builder, mock
    notifier/      interface, mocks
  workflows/
    order_push/    scheduler, poll, gate, push, notes reader and eval,
                   resolver, team queue and its command line, demo day
configs/demo/      rules.yaml, settings.yaml
evals/notes/       eval cases and saved results
tests/             incident tests, one per real failure or near-miss
docs/              decisions, components, open questions, production path
```

## Status
A demo. Mock systems and synthetic data. See
[production-path.md](production-path.md) for what running it for real
involves, and [open-questions.md](open-questions.md) for what is still
unknown about the real systems.

## How it was built
Built from a written brief. Each design decision was reviewed before it was
implemented, and is recorded in [decisions.md](decisions.md). Much of the
code was written with Claude Code.
