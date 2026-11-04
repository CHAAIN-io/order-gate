# Path to production

This repo is a demo. It runs against mocks and synthetic data, and mocks
carry their builder's assumptions. Going live is a separate project with its
own validation, not a switch on this code. This page covers what that
project would involve.

## 1. Close the unknowns first

A 20-minute call with the Ops lead and the Finance lead settles the three
questions that change the design most:
- Are warehouse orders still drafts, or confirmed on arrival?
- What form is the credit-hold list in, and would the Finance lead set the
  flag in the stock system themselves?
- Can the integration write to the sale note?

Then the warehouse contact, about lookup by doc ref and the warehouse's own
order ID. The full list is in [open-questions.md](open-questions.md).

## 2. Real connectors

| Connector | Work | Watch for |
|---|---|---|
| Stock system | An API client behind the existing interface. Map the real payload, including every fulfilment, into the sale model | A rate limit shared with two other integrations. Check it before shadow doubles the polling |
| Warehouse | OAuth2 client credentials, the real endpoint, and the response parsing | TEST and LIVE behave differently. Validate against LIVE with a known test order. A schema version change is pending |
| Notifier | Email from the client's own mailbox, through its Microsoft 365 tenant | Confirm which channel the live alerts use today, and move them |
| Credit hold | Read the stock system's credit-hold flag once per run | Confirm it is in the API, on the sale or the customer |

## 3. Hosting

The client appears to have a Google Cloud project with billing; confirm whose
it is. Hosting there gives the client ownership, which matters for handover.

- **Run:** a container, triggered by Cloud Scheduler on the polling windows
  and on the resolver's 15-minute cycle (or one small always-on service).
- **Store:** Postgres (Cloud SQL) behind the existing ledger repository. Run
  the ledger tests against it, including two runs at once.
- **Deploy:** from GitHub Actions, using workload identity federation, so
  there is no long-lived cloud key in the repository settings.
- **Cost:** a fixed monthly figure, quoted up front. The client wants costs
  that don't need thinking about.

## 4. Secrets and identity

Prefer identities over static keys wherever the platform supports it.
- The service runs as its own service account. It reaches the database and
  the secret store through that identity, with no keys in the code or the
  image.
- The stock system's API key and the warehouse's OAuth client secret only
  exist as static secrets. They go in Secret Manager, readable only by the
  service account, and are rotated on a schedule.
- Email through Microsoft Graph: an app registration with a federated
  credential trusting the service account, if the tenant allows it.
  Otherwise a certificate, not a client secret. Who administers the
  client's Microsoft tenant is unknown, possibly the IT contractor.
- AI (only if switched on, see 6): call the model through the client's own
  cloud (Claude on Vertex AI) under the service account, so there is no API
  key at all. The alternative is a client-owned model account with a spend
  cap. Either way it's one new provider in `llm/`, with nothing else
  changing.
- `.env` stays a local development convenience only.

## 5. Monitoring and alerts

- **Heartbeat:** each run reports in. If no run is recorded within the
  expected interval, someone is alerted. A crashed service sends no failure
  email on its own.
- **Run failures:** auth failures, stop-the-run outcomes, and connector errors
  alert immediately.
- **Queue age:** CHECK items, and holds near the cut-off, alert once. A
  reminder follows if nobody answers within a few hours (planned).
- **Daily summary:** sent, held, skipped and refused, to the orders inbox.
- **Logs:** structured, one line per decision with the order number, kept
  for at least a month-end stock reconciliation.

## 6. AI: off at go-live, switched on only if it earns it

The notes rule goes live in `report` mode. During the shadow run, count how
often a note should have changed what happened. If that rate justifies the
cost and another processor of customer data, switch the rule to `hold`.
Before that, re-run the eval with the production model. See decision 16.

## 7. The review interface

**First release:**
- Hold reasons are written into the sale note in the stock system.
- The Ops lead releases an order with a code word, the reverse of #NOVISION.
- A plain alert email goes to the orders inbox.

This needs nothing new to learn. The queue's actions (approve, reject,
received, retry) map onto code words, with the same logging.

**Later:** a web UI to monitor the ledger, the queue and the alerts in one
place, with sign-in so every action is tied to a real person. Today the
command line takes a typed name on trust.

## 8. Cutover: never two senders

A duplicate sender means a duplicate delivery, so the order of these steps
is fixed.

1. **Freeze the rules.** Finish customer contact tagging and any pending n8n
   versions first.
2. **Shadow run, 2 to 3 weeks.** The new service polls and decides, and
   builds the XML, but never sends. Each day, compare it against the n8n
   log: the same orders, the same skips, and equivalent XML. That's roughly
   150 to 250 orders. Don't move on until a #NOVISION sale, a Site-B pick
   and a split invoice have all been seen.
3. **Migrate the dedup log.** Freeze n8n, import its log with both the sale
   ID and the order number, reconcile the row counts, then switch on.
4. **Live, with n8n on standby for about 4 weeks.** Keep it deactivated but
   intact for rollback. Decommission it after one clean month-end stock
   reconciliation.

**Timing:** after the seasonal peak, not during it. The warehouse also has
blackout periods for testing over Christmas.

## 9. Running it

- **Runbook:** how to read the queue, clear a CHECK, pause sending, roll
  back to n8n, and rotate each secret. Written for the IT contractor, not
  for the author.
- **Response time:** a missed push means a missed delivery. Agree how
  quickly someone responds, and name a backup, before go-live.
- **Data protection:** customer addresses and phone numbers move to a new
  host. Check the client's privacy notice covers it.
