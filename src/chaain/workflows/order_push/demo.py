"""A simulated day: `make demo`.

Wednesday 08:00 to 13:30, through both cut-offs (Stage 3.2). About 20
orders arrive from the mock stock system: each past incident once, and
plain orders that should just go. The poll runs on the real timetable and
the resolver every 15 minutes. Every decision prints with its reason, then
the team queue.

Repeatable: a fixed seed and a simulated clock give the same day every
time. Offline: the notes reader replays the answers the real model gave in
the notes-v2 eval run (D1), so there's no key, network or spend, and no
different answer on the day. Afterwards `make queue` opens on this same day
(D4).
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from chaain.connectors.notifier.interface import Notifier
from chaain.connectors.notifier.mock import MemoryNotifier
from chaain.connectors.stock_system.generator import Customer, SaleGenerator
from chaain.connectors.stock_system.mock import MockStockSystem
from chaain.connectors.warehouse.mock import Behaviour, MockWarehouse
from chaain.core.config import load_rules, load_settings
from chaain.core.decision import Decision
from chaain.core.ledger import Ledger, State
from chaain.core.ledger.sqlite import SqliteLedger
from chaain.core.models import Sale, SaleStatus
from chaain.llm import LLMError
from chaain.llm.replay import ReplayProvider
from chaain.workflows.order_push.gate import GateRules, NotesResult, Reference, gate_for
from chaain.workflows.order_push.notes_eval import CASES, RESULTS, load_cases
from chaain.workflows.order_push.notes_reader import FRAMING, NotesReader, changed_text
from chaain.workflows.order_push.queue import Cutoffs, build_queue
from chaain.workflows.order_push.resolver import ResolverSettings, resolve, resolver_schedule
from chaain.workflows.order_push.run import RunReport, RunSettings, run_once
from chaain.workflows.order_push.scheduler import Schedule

UK = ZoneInfo("Europe/London")
DAY = date(2026, 11, 4)  # a Wednesday
START, END = time(8, 0), time(13, 30)
SEED = 7
REPLAY = RESULTS / "20261103-170129-notes-v2-claude-opus-5-5.json"


@dataclass(frozen=True)
class Order:
    at: str  # local HH:MM it lands in the stock system
    scenario: str  # from the sale generator
    story: str  # one line for the walkthrough
    note_case: str | None = None  # an eval case whose note this order carries
    warehouse: Behaviour | None = None  # what the warehouse does with it, if not accept
    credit_hold: bool = False  # the Finance lead has set the customer's credit-hold flag


@dataclass(frozen=True)
class Change:
    at: str
    order: int  # index into ORDERS
    story: str
    status: SaleStatus | None = None  # None: a payment sync, nothing about the order changes


ORDERS = (
    Order("08:05", "standard", "a plain order"),
    Order("08:20", "site_b_pick", "header says Warehouse-3PL, every line picked at Site-B (past incident)"),
    Order("08:35", "standard", "a note that adds nothing", note_case="as-per-usual"),
    Order("08:50", "mixed_pick", "some lines at Warehouse-3PL, some at Site-B"),
    Order("09:10", "not_invoiced", "not invoiced yet"),
    Order("09:25", "shopify_warehouse_pick", "Shopify order switched to warehouse stock (past discrepancy)"),
    Order("09:40", "novision_note", "#NOVISION: a re-bill for goods already delivered"),
    Order("09:55", "standard", "a plain order the warehouse will refuse",
          warehouse=Behaviour.reject("Validation failed: DeliveryPostcode invalid")),
    Order("10:10", "standard", "a delivery time written in the note", note_case="before-10am"),
    Order("10:20", "old_invoice", "invoiced 10 days ago, pulled back by a payment sync"),
    Order("10:30", "accented_text", "accents, & and apostrophes (past LIVE-vs-TEST failure)"),
    Order("11:05", "new_customer", "first order from a new customer"),
    Order("11:15", "standard", "a plain order; the warehouse gets it but never answers",
          warehouse=Behaviour.timeout(received=True)),
    Order("11:30", "below_minimum", "12 bottles to London, below the minimum"),
    Order("11:45", "internal_customer", "Warehouse-3PL as the customer: a stock correction"),
    Order("11:55", "standard", "a note that tries to instruct the AI", note_case="injection"),
    Order("12:10", "missing_town", "delivery address with no town"),
    Order("12:20", "standard", "a one-off delivery address in the note", note_case="other-site-this-week"),
    Order("12:40", "standard", "a plain order after the customer cut-off"),
    Order("13:10", "standard", "a plain order after the warehouse cut-off"),
    # Added last so the SO numbers above stay as they are in the walkthrough.
    Order("11:20", "standard", "a customer the Finance lead has put on credit hold", credit_hold=True),
)
CHANGES = (
    Change("10:40", 4, "invoiced", SaleStatus.INVOICED),
    Change("10:50", 0, "payment sync: it reappears in the poll"),
)


@dataclass
class Workspace:
    ledger: Ledger
    stock: MockStockSystem
    warehouse: MockWarehouse
    gate: Callable[[Sale], Decision]
    notifier: Notifier
    cutoffs: Cutoffs
    resolver: ResolverSettings
    run: RunSettings
    clock: Callable[[], datetime]


def play(out: Callable[[str], None] = print, notifier: Notifier | None = None) -> Workspace:
    """Play the day, printing it through `out`, and return it ready to act on."""
    settings, rules = load_settings(), load_rules()
    start, end = (datetime.combine(DAY, t, UK) for t in (START, END))
    now = [start]
    ledger = SqliteLedger(clock=lambda: now[0])
    stock = MockStockSystem()
    notifier = notifier or MemoryNotifier()

    gen = SaleGenerator(seed=SEED, now=start)
    notes = {c.id: c.note for c in load_cases(CASES)}
    sales: list[Sale] = []
    for o in ORDERS:
        # A credit hold applies to every order of that customer, so give it
        # a customer with no other order today, or they'd be held too.
        others = {s.customer_id for s in sales}
        customer = next(c for c in gen.customers if c.id not in others) if o.credit_hold else None
        sales.append(_sale(gen, o, notes, customer))
    warehouse = MockWarehouse(
        today=DAY, planned={s.so_number: o.warehouse for s, o in zip(sales, ORDERS) if o.warehouse},
    )
    ref = Reference(
        credit_hold_customers=frozenset(s.customer_id for s, o in zip(sales, ORDERS) if o.credit_hold),
        warehouse_stock={line.sku: 1000 for sale in sales for line in sale.lines},  # stock isn't today's story
        known_customers=frozenset(c.id for c in gen.customers),
    )
    gate = gate_for(ref, GateRules.from_config(rules), warehouse.prepare, _replayed_notes(notes))
    poll = Schedule.from_config(settings["scheduler"])
    resolver = ResolverSettings.from_config(settings)
    resolver_due = resolver_schedule(resolver, settings["scheduler"]["timezone"])
    run = RunSettings.from_config(settings["scheduler"])
    cutoffs = Cutoffs.from_config(settings)

    out(f"Client-Wine, {start:%a %d %b %Y}, {START:%H:%M} to {END:%H:%M} (simulated, seed {SEED}).")
    out("The poll follows the real timetable: every 15 minutes, every 5 from 11:00. The resolver runs every 15.")
    out("Notes are read with answers the real model gave in the notes-v2 eval, replayed offline.\n")

    events = [(_local(o.at), "order", i) for i, o in enumerate(ORDERS)]
    events += [(_local(c.at), "change", c) for c in CHANGES]
    events += [(datetime.combine(DAY, t, UK), "cutoff", label) for t, label in (
        (cutoffs.customer, f"customer cut-off: customers are told {cutoffs.customer:%H:%M}"),
        (cutoffs.warehouse, f"warehouse cut-off: anything sent from now goes tomorrow"),
    )]
    last_poll = start - timedelta(minutes=1)
    told: set[str] = set()  # ignored orders, said once
    alerts = 0
    t = start
    while t <= end:
        now[0] = t
        for at, kind, what in sorted(events, key=lambda e: e[0]):
            if at != t:
                continue
            if kind == "order":
                sale = sales[what]
                stock.add(sale)
                out(_line(t, sale.so_number, "arrives", ORDERS[what].story))
            elif kind == "change":
                sale = sales[what.order]
                changes = {"status": what.status, "invoice_date": t} if what.status else {}
                stock.update(sale.id, t, **changes)
                out(_line(t, sale.so_number, "changes", what.story))
            else:
                out(_line(t, "---------", "cut-off", what))
        if poll.is_due(t):
            report = run_once(t, stock, ledger, warehouse, gate, run)
            for line in _poll_lines(report, stock, ledger, last_poll, t, cutoffs, told):
                out(line)
            last_poll = t
        if resolver_due.is_due(t):
            r = resolve(t, ledger, warehouse, notifier, resolver)
            for so in r.made_check:
                out(_line(t, so, "check", "resolver: claimed with no outcome recorded; it may have arrived"))
            if r.alerted:
                alerts += 1
                verb = "needs" if len(r.alerted) == 1 else "need"
                out(_line(t, "", "alert", f"to the team: {', '.join(r.alerted)} {verb} a person"))
        t += timedelta(minutes=1)

    out("")
    out(_summary(ledger, len(ORDERS), alerts))
    started = datetime.now(timezone.utc)
    return Workspace(
        ledger, stock, warehouse, gate, notifier, cutoffs, resolver, run,
        clock=lambda: end + (datetime.now(timezone.utc) - started),  # moves with real time from here
    )


def _local(hhmm: str) -> datetime:
    return datetime.combine(DAY, time.fromisoformat(hhmm), UK)


def _sale(gen: SaleGenerator, order: Order, notes: dict[str, str], customer: Customer | None = None) -> Sale:
    at = _local(order.at)
    gen.now = at
    sale = gen.make(order.scenario, customer)
    if order.note_case:
        sale = replace(sale, note=notes[order.note_case])
    if order.scenario == "accented_text":
        # The point here is the XML. Its note is the customer's standing one,
        # so there's no order text for the (replayed) notes reader to read.
        sale = replace(sale, standing_note=sale.note)
    return replace(sale, last_modified=at)


def _replayed_notes(notes: dict[str, str]) -> Callable[[Sale], NotesResult | None]:
    """The notes reader, answering from the notes-v2 eval run.

    Standing comments aren't read here: the run has no answers for the demo
    customers' ones, and they only ever report, never hold."""
    saved = json.loads(REPLAY.read_text())
    version = saved["prompt_version"]
    answers = {}
    for a in saved["answers"]:
        if a["run"] != 1 or "flags" not in a or a["case"] not in notes:
            continue
        if a["note"] != notes[a["case"]]:
            raise ValueError(f"eval case {a['case']} has changed since {REPLAY.name}: re-run the eval")
        answers[FRAMING[version][0].format(a["note"].strip())] = {
            "flags": a["flags"], "explanation": a["explanation"],
        }
    provider = ReplayProvider(answers, model=saved["source"].rsplit("/", 1)[-1])
    reader = NotesReader(provider, sleep=lambda s: None, prompt_version=version)

    def read(sale: Sale) -> NotesResult | None:
        text = changed_text(sale, reader.order_label)
        if not text:
            return None
        try:
            return NotesResult(flags=reader.ask(text), source=reader.source())
        except LLMError as e:
            return NotesResult(error=str(e), source=reader.source())
    return read


def _poll_lines(
    report: RunReport, stock: MockStockSystem, ledger: Ledger, last_poll: datetime, t: datetime, cutoffs: Cutoffs,
    told: set[str],
) -> list[str]:
    lines, acted = [], set()
    reports: dict[str, list[str]] = {}
    for so, action, why in report.actions:
        acted.add(so)
        if action == "reported":
            reports.setdefault(so, []).append(why)
            continue
        if action == "ignored":
            if so in told:
                continue
            told.add(so)
        if action == "sent":
            why += _cutoff_note(t, cutoffs)
        lines.append(_line(t, so, action, why))
        lines += [_line(t, "", "", f"also noted: {r}") for r in reports.pop(so, [])]
    # New since the last poll, but nothing to do: say why, once.
    for sale in stock.list_sales_updated_since(last_poll + timedelta(minutes=1)):
        if sale.so_number in acted or sale.last_modified > t:
            continue
        entry = ledger.find(sale.id, sale.so_number)
        if entry is not None and entry.state is State.SENT:
            lines.append(_line(t, sale.so_number, "left alone", "already sent: dedup runs before the cap"))
        elif entry is None and sale.status is not SaleStatus.INVOICED:
            lines.append(_line(t, sale.so_number, "waiting", f"{sale.status.value}, not invoiced: looked at again next poll"))
    if report.deferred:
        lines.append(_line(t, "", "deferred", f"next poll: {', '.join(report.deferred)}"))
    if report.stopped:
        lines.append(_line(t, "", "stopped", report.stopped))
    return lines


def _line(t: datetime, so: str, action: str, why: str) -> str:
    head = f"{t:%H:%M}  {so:<9}  {action:<10} "
    return textwrap.fill(why, width=110, initial_indent=head, subsequent_indent=" " * len(head))


def _cutoff_note(t: datetime, cutoffs: Cutoffs) -> str:
    local = t.astimezone(cutoffs.tz).time()
    if local >= cutoffs.warehouse:
        return f" (after the {cutoffs.warehouse:%H:%M} warehouse cut-off: goes tomorrow)"
    if local >= cutoffs.customer:
        return f" (after {cutoffs.customer:%H:%M}, but the warehouse accepts until {cutoffs.warehouse:%H:%M}: goes today)"
    return ""


def _summary(ledger: Ledger, orders: int, alerts: int) -> str:
    counts = {s: len(ledger.in_state(s)) for s in (State.SENT, State.HELD, State.SKIPPED, State.REJECTED, State.CHECK)}
    rows = sum(counts.values())
    parts = [f"{counts[s]} {label}" for s, label in (
        (State.SENT, "sent"), (State.HELD, "held for a person"), (State.SKIPPED, "skipped"),
        (State.REJECTED, "refused by the warehouse"), (State.CHECK, "may or may not have arrived"),
    )]
    return (f"By {END:%H:%M}, {orders} orders: " + ", ".join(parts) + f", {orders - rows} ignored.\n"
            + f"Alerts to the team: {alerts}. Live model calls: 0.")


def main() -> None:
    from chaain.workflows.order_push.queue_cli import render_queue

    ws = play()
    at = ws.clock()
    print("\n" + render_queue(build_queue(at, ws.ledger, ws.stock, ws.resolver.stale_pending_minutes, ws.cutoffs.tz),
                              at, ws.cutoffs))
    print('\nAct on this queue: make queue AS="Ops lead"')


if __name__ == "__main__":
    main()
