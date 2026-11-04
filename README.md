# Order gate

Decides which wholesale orders go from a stock system to a third-party
warehouse automatically, which a person must check first, and what to do
when nobody knows whether the warehouse received one. It is a rebuild in
code of a live n8n integration for a wine importer. This version runs on
mocks and synthetic data. (`chaain` is the author's consultancy, and the
package name.)

## What the live version does
On a schedule, n8n polls a Cin7-style stock system for invoiced sales. It
skips Shopify orders, orders for Site-B (by header and by pick location) and
the warehouse's own account, and, since October, invoice-only sales marked
#NOVISION. It checks a log so nothing is sent twice, builds EDI XML, sends it
to the warehouse (Vision-style API) and emails alerts on failures. Live since
mid-August 2026; the client's team reports it saves about 20 minutes a day.

## What went wrong, and why
| What happened | Cause |
|---|---|
| The warehouse's LIVE system rejected XML that its TEST system had accepted | Single quotes and the case of "utf-8" in the XML declaration |
| Orders were missed by the poll | A per-run cap was applied before deduplication, so already-sent orders used it up |
| A Site-B stock order was sent to the warehouse | The header said warehouse; the stock was picked at Site-B. Fixed by checking the pick location |
| October: a customer received the same order twice | The send succeeded, the log write was rate-limited and failed, and the next run sent it again |
| Near-misses: the first run would have resent orders already keyed in by hand; a hand-entered log row didn't block a resend | The lookback crossed the manual-to-automatic boundary; the row's ID was cut short on paste |

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
notes, 3 runs each: no missed holds and no wrong holds. One ambiguous note
("Tues or Thurs only") was held each time, which counts as fair. In
production it would start in report-only mode and hold only if it earns it.

## What it looks like
From `make demo`, abridged:
```
08:15  SO-10001   sent       accepted by the warehouse
08:30  SO-10002   skipped    Order picked at Site-B: not for the warehouse
10:15  SO-10009   held       Note: time in instructions: The note asks for delivery before 10am, ...
                             [notes-v2 via replay/claude-opus-5-5]
11:00  SO-10001   left alone already sent: dedup runs before the cap
11:15  SO-10013   check      no answer within 60s; it may have arrived
11:15             alert      to the team: SO-10012, SO-10013 need a person
11:20  SO-10021   held       Customer is on credit hold: the Finance lead decides
13:10  SO-10020   sent       accepted by the warehouse (after the 13:00 warehouse cut-off: goes tomorrow)
```

## Run it
Needs Python 3.12. Everything runs offline against mocks.
```
make setup
make test                    # 300+ tests, no network
make demo                    # a simulated Wednesday: 21 orders, then the team queue
make queue AS="Ops lead"     # act on that queue: list, show, approve, reject, retry
make eval-score              # re-score the saved notes eval, no API call
make eval                    # re-run it against the model (needs ANTHROPIC_API_KEY in .env)
```

## Known limitations
- Mocks carry assumptions. The warehouse's real behaviour (lookup by doc
  ref, an asynchronous acknowledgement, format limits) is unconfirmed:
  [open-questions.md](docs/open-questions.md).
- No real connectors, hosting, sign-in or monitoring yet:
  [production-path.md](docs/production-path.md).
- The queue is a command line that takes a typed name on trust.
- The demo replays recorded model answers, so it can only show the notes
  that were recorded.

More: [design](docs/DESIGN.md), [decisions](docs/decisions.md),
[components](docs/components.md).
