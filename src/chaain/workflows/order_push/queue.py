"""The team queue: everything waiting for a person, and what they can do.

Order (Stage 3.1, D1): first what may or may not be at the warehouse
(CHECK, and PENDING past the stale limit), then held orders, then orders
the warehouse refused. Oldest first within each. Each item shows every
reason with its evidence, and a CHECK ends with a plain question.

Actions, each logged on the row with who and when:
- approve (HELD): send now, unless the sale changed since it was held, in
  which case it's gated again and shown again (D2).
- reject (HELD): final; the order is never reconsidered (D3). If you're not
  sure yet, leave it held.
- received (CHECK): a person found it at the warehouse.
- retry (CHECK or REJECTED): it isn't at the warehouse (or was fixed at
  source), so the next poll gates and sends it again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from chaain.connectors.stock_system.interface import StockSystem
from chaain.connectors.warehouse.interface import Warehouse
from chaain.core.decision import Decision, Route
from chaain.core.ledger import Entry, Ledger, LedgerError, State
from chaain.core.models import Sale
from chaain.workflows.order_push.push import fingerprint, push
from chaain.workflows.order_push.resolver import ALERTED

UNKNOWN, HELD, REFUSED = 1, 2, 3
SECTIONS = {
    UNKNOWN: "May or may not be at the warehouse",
    HELD: "Held for a decision",
    REFUSED: "Refused by the warehouse",
}
ACTIONS = {
    State.CHECK: ("received", "retry"),
    State.PENDING: (),
    State.HELD: ("approve", "reject"),
    State.REJECTED: ("retry",),
}
CHANGED = "changed since it was held: review the new reasons, then approve again"


class QueueError(Exception):
    """The action can't be done; nothing was changed."""


@dataclass(frozen=True)
class Cutoffs:
    customer: time  # what customers are told
    warehouse: time  # what the warehouse really accepts
    tz: ZoneInfo

    @classmethod
    def from_config(cls, settings: dict) -> "Cutoffs":
        q = settings["queue"]
        return cls(
            time.fromisoformat(q["customer_cutoff"]), time.fromisoformat(q["warehouse_cutoff"]),
            ZoneInfo(settings["scheduler"]["timezone"]),
        )

    def describe(self, now: datetime) -> str:
        local = now.astimezone(self.tz)
        customer = datetime.combine(local.date(), self.customer, self.tz)
        warehouse = datetime.combine(local.date(), self.warehouse, self.tz)
        if local < customer:
            return f"{_duration(customer - local)} to the {self.customer:%H:%M} cut-off"
        if local < warehouse:
            return (f"past the {self.customer:%H:%M} cut-off customers are told; the warehouse "
                    f"still accepts until {self.warehouse:%H:%M} ({_duration(warehouse - local)} left)")
        return f"past today's {self.warehouse:%H:%M} warehouse cut-off: anything sent now goes tomorrow"


@dataclass(frozen=True)
class Item:
    so_number: str
    state: State
    section: int
    customer: str
    since: datetime
    reasons: tuple[str, ...]
    evidence: tuple[str, ...]
    question: str | None
    actions: tuple[str, ...]


def build_queue(
    now: datetime, ledger: Ledger, stock: StockSystem, stale_pending_minutes: int = 30,
    tz: ZoneInfo = ZoneInfo("Europe/London"),
) -> list[Item]:
    stale = now - timedelta(minutes=stale_pending_minutes)
    entries = ledger.in_state(State.CHECK, State.HELD, State.REJECTED) + [
        e for e in ledger.in_state(State.PENDING) if e.updated_at <= stale
    ]
    items = [_item(e, ledger, stock, tz) for e in entries]
    return sorted(items, key=lambda i: (i.section, i.since, i.so_number))


def _item(entry: Entry, ledger: Ledger, stock: StockSystem, tz: ZoneInfo) -> Item:
    history = ledger.history(entry.so_number)
    sale = stock.get_sale(entry.guid) if entry.guid else None
    customer = sale.customer_name if sale else (entry.customer_id or "unknown customer")
    notes = [_event(e, tz) for e in history if e.is_note and e.reason != ALERTED]
    so = entry.so_number

    if entry.state is State.HELD:
        latest = next((e for e in reversed(history) if e.details and "reasons" in e.details), None)
        reasons = tuple(latest.details["reasons"]) if latest else (entry.reason,)
        return Item(so, entry.state, HELD, customer, entry.updated_at, reasons, tuple(notes), None, ACTIONS[State.HELD])

    if entry.state is State.REJECTED:
        return Item(
            so, entry.state, REFUSED, customer, entry.updated_at, (entry.reason,), tuple(notes),
            "Fix the order in the stock system, then mark it for retry.", ACTIONS[State.REJECTED],
        )

    # CHECK, or PENDING past the stale limit
    evidence = [*notes]
    sends = [e for e in history if e.to_state in (State.PENDING, State.CHECK) and not e.is_note]
    for e in sends:
        evidence.append(_event(e, tz) + _details(e.details))
    question = (
        f"Search Warehouse-3PL for doc ref {so}. Found it? Mark received. "
        "Not there? Mark for retry, and it goes again on the next poll."
    )
    if entry.state is State.PENDING:
        question = "The resolver moves this to CHECK on its next run. " + question
    return Item(so, entry.state, UNKNOWN, customer, entry.updated_at, (entry.reason,), tuple(evidence),
                question, ACTIONS[entry.state])


def _event(e, tz: ZoneInfo) -> str:
    return f"{e.at.astimezone(tz):%a %d %b %H:%M} {e.actor}: {e.reason}"


def _details(details: dict | None) -> str:
    if not details or "transport" not in details:
        return ""
    bits = [f"transport {details['transport']}"]
    if details.get("status"):
        bits.append(f"status {details['status']}")
    if details.get("elapsed_seconds"):
        bits.append(f"after {details['elapsed_seconds']:.0f}s")
    return " (" + ", ".join(bits) + ")"


def _duration(d: timedelta) -> str:
    minutes = int(d.total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


# ---------- actions ----------

def approve(
    so_number: str, actor: str, *, ledger: Ledger, stock: StockSystem, warehouse: Warehouse,
    gate: Callable[[Sale], Decision],
) -> str:
    entry = _entry(ledger, so_number, State.HELD, actor)
    sale = stock.get_sale(entry.guid) if entry.guid else None
    if sale is None:
        raise QueueError(f"{entry.so_number}: the stock system no longer has this sale")
    if fingerprint(sale) != entry.fingerprint:
        return _regate(entry, sale, actor, ledger, gate)
    result = push(sale, ledger, warehouse, actor=actor, reason=f"approved by {actor}")
    if not result.attempted:
        raise QueueError(f"{entry.so_number}: {result.reason}")
    return f"{entry.so_number}: {result.state.value if result.state else 'unknown'}. {result.reason}"


def _regate(entry: Entry, sale: Sale, actor: str, ledger: Ledger, gate: Callable[[Sale], Decision]) -> str:
    """The sale changed since the person saw it: show it again, not send it."""
    decision = gate(sale)
    if decision.route is Route.SKIP:
        ledger.move(entry.so_number, State.SKIPPED, f"changed since it was held; now skipped: {decision.summary}",
                    actor, final=decision.final)
        return f"{entry.so_number}: not sent. It changed since it was held and is now skipped: {decision.summary}"
    reasons = list(decision.reasons) or ["passes every check now"]
    ledger.annotate(entry.so_number, CHANGED, actor, details={"reasons": reasons}, fingerprint=fingerprint(sale))
    return f"{entry.so_number}: not sent. It {CHANGED}."


def reject(so_number: str, actor: str, why: str, *, ledger: Ledger) -> str:
    if not why.strip():
        raise QueueError("say why it's rejected: the reason goes in the audit trail")
    entry = _entry(ledger, so_number, State.HELD, actor)
    _move(ledger, entry, State.SKIPPED, f"rejected by {actor}: {why.strip()}", actor, final=True)
    return f"{entry.so_number}: rejected for good. It won't be reconsidered."


def mark_received(so_number: str, actor: str, *, ledger: Ledger, note: str = "") -> str:
    entry = _entry(ledger, so_number, State.CHECK, actor)
    _move(ledger, entry, State.SENT, _with_note(f"found at the warehouse by {actor}", note), actor)
    return f"{entry.so_number}: marked received. It won't be sent again."


def mark_retry(so_number: str, actor: str, *, ledger: Ledger, note: str = "") -> str:
    entry = _entry(ledger, so_number, (State.CHECK, State.REJECTED), actor)
    why = (f"confirmed NOT at the warehouse by {actor}" if entry.state is State.CHECK
           else f"fixed at source, cleared for retry by {actor}")
    _move(ledger, entry, State.RETRY, _with_note(why, note), actor)
    return f"{entry.so_number}: marked for retry. The next poll gates it and sends it if it passes."


def _entry(ledger: Ledger, so_number: str, expected: State | tuple[State, ...], actor: str) -> Entry:
    if not actor or not actor.strip():
        raise QueueError("every action needs a name: who is doing this?")
    entry = ledger.find(so_number=so_number)
    if entry is None:
        raise QueueError(f"{so_number}: not in the ledger")
    allowed = expected if isinstance(expected, tuple) else (expected,)
    if entry.state not in allowed:
        names = " or ".join(s.value for s in allowed)
        raise QueueError(f"{entry.so_number} is {entry.state.value}, not {names}: someone may have acted on it already")
    return entry


def _move(ledger: Ledger, entry: Entry, to: State, reason: str, actor: str, final: bool = False) -> None:
    try:
        ledger.move(entry.so_number, to, reason, actor, final=final)
    except LedgerError as e:  # e.g. someone else acted between our read and our write
        raise QueueError(f"{entry.so_number}: not changed ({e})") from e


def _with_note(reason: str, note: str) -> str:
    return f"{reason}: {note.strip()}" if note.strip() else reason
