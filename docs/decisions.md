# Decisions

One entry per decision that shaped the system: the context, what was chosen,
what was rejected, and what it costs. Newest last. Where a decision came from
a production incident, the incident is named; see the README for the full
history.

---

### 1. Rules decide; AI only reads notes, and can only hold
**Context.** Sending an order is hard to reverse: the warehouse confirms and
picks it on arrival. Most checks (location, channel, credit, minimums) are
clear-cut. Free-text notes are not.
**Decision.** Routing is deterministic rules. One AI step, the notes reader,
turns free text into flags with a confidence score. A raised flag, or an
unsure one, holds the order for a person. AI never sends, skips or releases.
**Alternatives.** AI routing the whole order; no AI at all.
**Consequences.** The worst an AI error can do is hold an order that was
fine. That costs a person a minute. A missed flag is the remaining risk,
which the eval set (14) measures.

### 2. At most once: claim before sending, never retry a send automatically
**Context.** October: the warehouse accepted an order, the log write that
followed was rate-limited and failed, and the next run sent it again. The
customer got two deliveries.
**Decision.** Each order is claimed in the ledger (PENDING) before it is
sent; if the claim fails, nothing is sent. No send is ever retried by the
system. When the outcome is unclear, the order goes to CHECK and a person
decides.
**Alternatives.** Retry with backoff; log after sending (the live design
before the fix).
**Consequences.** An unclear send can leave an order waiting for a person
until someone checks. That is slower, but the failure is visible and
recoverable, where a duplicate delivery is not.

### 3. What each warehouse answer means
**Context.** The warehouse confirms receipt only in its immediate HTTP
response. There is no later acknowledgement.
**Decision.** 200 "saved" → SENT. 400 and other 4xx → REJECTED. 401/403 →
REJECTED, and the run stops. 429, or couldn't connect → RETRY, because it
can't have arrived. Timeout (60s), dropped connection, 5xx, 408, a 200
without "saved", or our own write failing after a 200 → CHECK.
**Alternatives.** Treat timeouts as failures and resend (the source of
duplicate risk).
**Consequences.** Only answers that prove the order never arrived lead to a
resend. Everything else goes to a person.

### 4. Deduplicate before the per-run cap
**Context.** Go-live day: the cap of 10 orders per run was applied before
deduplication. Already-sent orders used it up, so 7 of 17 orders were never
looked at, and some arrived a day late.
**Decision.** Drop everything the ledger has handled first, then apply the
cap, oldest first. The rest wait for the next run.
**Consequences.** The cap only ever counts orders that need a decision. A
test reproduces the go-live day.

### 5. Ledger states: SENT or RETRY, never "resolved"
**Context.** The live team already speaks in SENT and RETRY when clearing a
stuck order.
**Decision.** Eight states (see DESIGN.md). A CHECK ends as SENT (it
arrived) or RETRY (a person confirmed it didn't). Only #NOVISION, credited
and voided sales are skipped for good. Location and channel skips are looked
at again when the sale changes, because Shopify orders are invoiced at once
and corrected afterwards. Sales that aren't invoiced get no row at all.
**Alternatives.** A single RESOLVED state.
**Consequences.** "What is at the warehouse?" is always just SENT. There is
no path back to PENDING from SENT or CHECK, which is what enforces 2.

### 6. Pick location per line, not per sale
**Context.** A stock order picked at Site-B was sent to the warehouse because
the header said Warehouse-3PL. The live build also reads only the first
fulfilment.
**Decision.** Each line carries its pick location and fulfilment number.
Every line at Site-B → SKIP. Some lines at Site-B → HOLD.
**Consequences.** Mixed and second-fulfilment orders are caught wherever the
Site-B lines are.

### 7. Values in config, logic in code, and a mode for every hold rule
**Context.** The team owns thresholds and lists. Behaviour changes need tests.
Not every check has been measured yet.
**Decision.** Values (locations, minimums, the confidence threshold, the
#NOVISION marker) live in `configs/<client>/`. Each hold rule has a mode:
hold, report or off. The defaults are a lean set: credit, stock, mixed
locations, Shopify at the warehouse, data defects, new customer and notes
hold. Vintage, reserved stock and minimums only report.
**Alternatives.** Rules as data (a mini-language in YAML); everything in code.
**Consequences.** Rules can be tuned without a developer. "Report everything,
count, then decide what holds" is how a shadow run works (see
production-path.md).

### 8. Poll more often near the cut-off
**Context.** Customers are told 12:30. The warehouse accepts until 13:00. A
hold at 12:20 matters far more than one at 09:00.
**Decision.** Every 15 minutes from 08:00 to 20:00, and every 5 from 11:00
to 13:30. Lookback 48 hours, invoices older than 7 days ignored, cap 10. All
in config.
**Consequences.** More calls to the stock system in the late morning. Its
rate limit is shared with other integrations, so this is an open question.

### 9. The warehouse format lives in the warehouse connector
**Decision.** The XML, field lengths, the 50-line limit and the escaping
rules belong to the warehouse connector, not to the workflow. An order the
warehouse would refuse is held with the reason before sending, and is not
retried every run.
**Consequences.** A different warehouse means a new connector, not a new
gate. The XML declaration that LIVE rejected (and TEST accepted) is pinned
by a test.

### 10. If the notes reader fails, retry once, then hold
**Decision.** A failed or malformed AI answer is retried once after a pause,
then the order is held with "read the note before sending". Reading is safe
to repeat; sending is not, which is why 2 forbids send retries. Standing
customer comments are read once and reported as "fix the customer record"
rather than holding every order.
**Consequences.** An AI outage holds orders that have notes; it never sends
them unread.

### 11. Measure the AI step with an eval set before trusting it
**Context.** The first prompt (notes-v1) held 18 of 21 safe notes across its
runs. Its own label, "changed for this order", read as the customer asking
for a one-off change.
**Decision.** 26 synthetic notes, each with flags the reader must raise and
flags it may raise. Three runs per note. The headline figure is whether each
note reaches a person when it should. The threshold suggested is the one with
no misses and the fewest wrong holds, with ties going higher. Results are
saved, and re-scored offline.
**Result.** notes-v2: no misses and no wrong holds (3 fair holds) on all 26
notes and 3 runs. The threshold stays at 0.8.
**Consequences.** Any change to the prompt, model or threshold is re-run
against the same set before it ships.

### 12. The resolver runs on its own schedule, around the clock
**Context.** The live stale-order check only runs when a poll finds recent
orders. An order stuck on Friday evening waits until Monday.
**Decision.** Every 15 minutes, every day. A claim with no outcome after 30
minutes becomes CHECK, and the team gets one alert per item per state.
**Consequences.** The Friday case alerts within the hour. No repeat alerts
yet; a reminder for items nobody has answered after a few hours is planned.

### 13. A warehouse lookup can confirm an order, never clear one for resend
**Decision.** Where the warehouse can be asked "do you have this doc ref?",
"found" moves CHECK to SENT automatically. "Not found" stays with a person,
recorded as evidence.
**Consequences.** An automatic yes can't cause a resend. An automatic no
could, if the warehouse hadn't imported the order yet. Lookup is off until
the real warehouse's behaviour is known.

### 14. The team queue: by risk, named actions, and approvals that check for changes
**Decision.** Orders that may or may not be at the warehouse come first,
then holds, then refusals, oldest first within each. Every action needs a
name, and is logged. Approving a hold checks the sale against the version
that was held; if what the warehouse acts on has changed, it goes through
the gate again and is shown again instead of being sent. Rejecting is final.
**Alternatives.** Send whatever is there on approval; non-final rejects
(which a payment sync could bring back).
**Consequences.** Someone who edits an order they've just approved has to
approve it twice. That is accepted, because approving one version isn't
approving the next.

### 15. The demo replays recorded model answers
**Decision.** `make demo` reads notes using the answers the real model gave
in the notes-v2 eval, replayed offline. `make test` never calls the API.
**Consequences.** It runs without a key, costs nothing, and gives the same
answers every time. It can only show notes that were recorded; anything else
fails, and is held.

### 16. In production, AI is off at go-live and earns its place
**Context.** Everything up to the send is deterministic. AI adds cost and
another processor of customer data.
**Decision.** Go live with the notes rule set to `report`. During the shadow
run, measure how often notes change what should happen. Switch it to `hold`
only if that's worth it.
**Consequences.** The system is complete without AI. The eval set is what
makes the later call evidence-based.

### 17. Review where the team already works now; a monitoring UI later
**Context.** The Ops lead works in the stock system and the orders inbox, on
a laptop and not always at a desk. A new tool is a new thing to learn and
maintain.
**Decision.** First release: write the hold reason into the sale note in the
stock system and release with a code word (the reverse of #NOVISION), plus a
plain alert email to the orders inbox. The command-line queue in this repo is
the demo's stand-in for the same actions. Later: a web UI to monitor the
ledger, queue and alerts.
**Consequences.** Nothing new to learn at go-live. It needs write access to
the stock system's sale note, which is unconfirmed. There is no single view
until the UI exists.

### 18. Small, standard tooling
**Decision.** Python 3.12, standard dataclasses (not Pydantic), pip with
exact pins, SQLite behind a repository interface, pytest. One SDK: the model
provider's.
**Consequences.** Little to install or explain. Postgres and a validating
model library can be added in the real connectors without changing core code.
