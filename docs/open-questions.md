# Open questions

What isn't known about the real systems, and what the demo assumes instead.
Each one needs an answer before, or during, a shadow run. Mocks carry
assumptions, and a gap between the warehouse's TEST and LIVE systems has
already caused one failure.

## The warehouse (Vision-style EDI)

| Question | Why it matters | Demo assumes |
|---|---|---|
| Can an order be looked up by doc ref? | Would let the resolver confirm unclear sends without a person (decision 13) | Lookup off. The code path exists and is tested |
| Is there any acknowledgement after the HTTP response, or a despatch confirmation? | Today a missing response leaves nothing to check against | No. The immediate response is all there is |
| Does a 200 include the warehouse's own order ID? | It would be the strongest proof of receipt, and could be stored on SENT | Not used |
| Are orders confirmed and picked on arrival, or do they land as drafts? | With drafts, a person already reviews every order, so a hold queue on top would duplicate that work. Without drafts, the gate's holds are the only safety net | Confirmed on arrival. Holds are the only safety net |
| Where do one-off delivery changes go? | Without drafts, emailed changes ("this week only, deliver to...") have nowhere to be applied before picking | Held for a person by the notes reader |
| What happens to orders with no delivery window? | One account says they're deprioritised to the afternoon; another says the warehouse books deliveries directly | The default window from config is sent |
| What are the real format limits? | Instruction length (120), lines per order (50) and the town rule are taken from the spec as understood | As stated, in the connector |
| Is a schema version change due? | A format change mid-cutover would mix two risks | No change |

## The stock system (Cin7-style)

| Question | Why it matters | Demo assumes |
|---|---|---|
| Is the credit-hold flag available through the API on the sale or customer? Will the Finance lead set and clear it? | The strongest gain over the live build is checking credit at the point of push | The flag exists and is read once per run |
| Can the integration write to the sale note? | The planned review interface writes the hold reason there (decision 17) | The command-line queue instead |
| What is the API rate limit, and who else shares it? | Three integrations poll the same account; a shadow run doubles the polling | No limit |
| Are per-order notes still read, or only the customer's standing comment? | Decides what the notes reader is given | Both: the sale note, compared with the standing comment |
| "RESERVE FOR" or "RESERVED FOR"? | The product flag and the stock sheet use different spellings | Product flags, reported only |
| Are the SKU suffixes other than RW real? | Vintage detection reads the SKU | Assumed |

## The business

| Question | Why it matters | Demo assumes |
|---|---|---|
| The live poll interval | It sets how quickly holds and failures surface | 15 minutes, 5 near the cut-off |
| Orders per weekday, and peak-day maximums | Sizes the per-run cap and the queue | About 15 a working day (inferred from the log) |
| Minimum-order bands outside London and Scotland | The minimum rule needs them | A default of 36 bottles |
| Does the order push need AI at all? | AI adds cost and another data processor | Off at go-live, measured in shadow (decision 16) |
| Who reviews unclear sends day to day, and how fast must they respond? | At-most-once relies on someone answering CHECK items | The Ops lead, through the queue |
| Who owns hosting, billing and the model account? | Handover, cost visibility, data protection | Not modelled |

## Answered during the build

- **Cut-offs.** Customers are told 12:30; the warehouse accepts until 13:00.
- **Final skips.** Only #NOVISION, credited and voided.
- **Minimums.** Below the area band and no delivery-charge line.
- **New customer.** No order sent for them before.
