# Order gate

Decides which wholesale orders go from a stock system to a third-party
warehouse automatically, which a person must check first, and what to do
when nobody knows whether the warehouse received one. It is a rebuild in
code of a live n8n integration for a wine importer. This version runs on
mocks and synthetic data.

## What the live version does
Every 15 minutes or so, n8n polls a Cin7-style stock system for invoiced
sales and skips Shopify, Site-B and invoice-only (#NOVISION) sales. It
checks a log so nothing is sent twice, builds EDI XML and sends it to the
warehouse (Vision-style API), then logs the result as SENT, REJECTED or
CHECK. Live since August 2026; it saves the Ops lead about 10 minutes a day.

## What went wrong, and why
| When | What happened | Cause |
|---|---|---|
| Before go-live | LIVE rejected XML that TEST had accepted | Quoting and case in the XML declaration |
| From go-live | 7 of 17 orders never looked at on go-live day; on busy days, orders went missing or arrived a day late | The cap of 10 per run was applied before deduplication, so already-sent orders used it up |
| After go-live | A Site-B stock order was sent to the warehouse | The header said warehouse; the lines were picked at Site-B |
| October | A customer received the same order twice | The warehouse accepted it, the log write was rate-limited and failed, and the next run sent it again |
| Near-misses | Resends after unclear responses; a hand-entered log row that didn't block a resend | Automatic retries; a truncated ID |
| Ongoing | A stuck order on Friday evening isn't flagged until Monday | The stale check only runs when new orders arrive |

Behind all of these: judgement that lived in the Ops lead's head (credit
holds, mixed picks, one-off delivery changes), and a warehouse that only
confirms receipt in its immediate response.

## What this rebuild changes
- **At most once:** claim in the ledger before sending, never retry a send,
  and send anything unclear to a person (CHECK). Deduplicate before the cap.
- **A gate:** skip, hold with every reason, or send. Credit hold, stock,
  mixed picks and new customers are checked before sending.
- **A resolver** that runs every 15 minutes around the clock, and a **team
  queue** where every action is logged with a name.
- **One test per incident**, plus the client's rules in config, not code.

## Where AI is and isn't used
Only to read free-text order notes, such as "deliver before 10am" or "this
week only, send to our other site". A flagged note holds the order for a
person. AI never sends, skips or releases anything. Measured on 26 synthetic
notes, 3 runs each: no missed holds and no wrong holds. In production it
would start in report-only mode and hold only if it earns it.

## Run it
Needs Python 3.12. Everything runs offline against mocks.
```
make setup
make test                    # 300+ tests, no network
make demo                    # a simulated Wednesday: 20 orders, then the team queue
make queue AS="Ops lead"     # act on that queue: list, show, approve, reject, retry
make eval-score              # re-score the saved notes eval, no API call
make eval                    # re-run it against the model (needs ANTHROPIC_API_KEY in .env)
```

## Known limitations
- Mocks carry assumptions. The warehouse's real behaviour (lookup by doc
  ref, drafts or auto-confirm, format limits) is unconfirmed:
  [open-questions.md](docs/open-questions.md).
- No real connectors, hosting, sign-in or monitoring yet:
  [production-path.md](docs/production-path.md).
- The queue is a command line that takes a typed name on trust.
- The demo replays recorded model answers, so it can only show the notes
  that were recorded.

More: [design](docs/DESIGN.md), [decisions](docs/decisions.md),
[components](docs/components.md).
