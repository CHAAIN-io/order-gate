"""One poll: fetch, filter, dedup, cap, gate, push.

The order of these steps is the fix for the go-live bug. The live workflow
once applied its cap of 10 before dedup, so orders that were already sent
used up the cap and new ones waited a day. Here dedup comes first, so the
cap only ever counts orders that genuinely need a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from chaain.connectors.stock_system.interface import StockSystem
from chaain.connectors.warehouse.interface import Warehouse
from chaain.core.decision import Decision, Route
from chaain.core.ledger import DuplicateEntry, Entry, Ledger, LedgerError, State
from chaain.core.models import Sale, SaleStatus
from chaain.workflows.order_push.push import fingerprint, push

Gate = Callable[[Sale], Decision]


@dataclass(frozen=True)
class RunSettings:
    lookback_hours: int = 48
    max_invoice_age_days: int = 7
    per_run_cap: int = 10

    @classmethod
    def from_config(cls, scheduler: dict) -> "RunSettings":
        return cls(scheduler["lookback_hours"], scheduler["max_invoice_age_days"], scheduler["per_run_cap"])


@dataclass
class RunReport:
    at: datetime
    seen: int = 0
    actions: list[tuple[str, str, str]] = field(default_factory=list)  # (so, action, why)
    deferred: list[str] = field(default_factory=list)  # over the cap: next run
    changed_after_send: list[str] = field(default_factory=list)  # for the team queue
    stopped: str | None = None

    def note(self, so: str, action: str, why: str) -> None:
        self.actions.append((so, action, why))

    def count(self, action: str) -> int:
        return sum(1 for _, a, _ in self.actions if a == action)


def run_once(
    now: datetime, stock: StockSystem, ledger: Ledger, warehouse: Warehouse, gate: Gate,
    settings: RunSettings = RunSettings(),
) -> RunReport:
    report = RunReport(at=now)
    sales = stock.list_sales_updated_since(now - timedelta(hours=settings.lookback_hours))
    report.seen = len(sales)

    # 1. Dedup first: drop anything the ledger says is handled.
    todo: list[tuple[Sale, Entry | None]] = []
    seen_so: set[str] = set()
    for sale in sales:
        so = sale.so_number.strip().upper()
        if so in seen_so:
            report.note(sale.so_number, "ignored", "same SO number twice in this run")
            continue
        seen_so.add(so)
        entry = ledger.find(sale.id, sale.so_number)
        if entry is not None and not _needs_decision(sale, entry):
            if entry.state is State.SENT and entry.fingerprint and entry.fingerprint != fingerprint(sale):
                report.changed_after_send.append(sale.so_number)
            continue
        # 2. Only then decide what's eligible at all. Not-ready sales get no
        #    row, so they're simply looked at again next time.
        if sale.status is not SaleStatus.INVOICED and entry is None:
            continue
        if _too_old(sale, now, settings) and entry is None:
            report.note(sale.so_number, "ignored", f"invoiced over {settings.max_invoice_age_days} days ago")
            continue
        todo.append((sale, entry))

    # 3. Cap after dedup, oldest first; the rest wait for the next run.
    todo.sort(key=lambda pair: pair[0].last_modified)
    now_batch, report.deferred = todo[: settings.per_run_cap], [s.so_number for s, _ in todo[settings.per_run_cap:]]

    # 4. Gate and act.
    for index, (sale, entry) in enumerate(now_batch):
        if entry is None:
            try:
                ledger.open(sale.so_number, sale.id, sale.customer_id, "first seen in poll")
            except DuplicateEntry:
                report.note(sale.so_number, "ignored", "another run recorded it first")
                continue
        try:
            _act(sale, gate(sale), ledger, warehouse, report)
        except LedgerError as e:
            report.note(sale.so_number, "ignored", f"ledger refused: {e}")
        if report.stopped:
            report.deferred += [s.so_number for s, _ in now_batch[index + 1:]]
            break
    return report


def _needs_decision(sale: Sale, entry: Entry) -> bool:
    """A row re-enters the gate if it was never decided (CANDIDATE: e.g. its
    claim failed, so nothing was sent), a person cleared it for retry, or it
    was a non-final skip and the sale has changed since (e.g. a corrected
    Shopify pick location)."""
    if entry.state in (State.CANDIDATE, State.RETRY):
        return True
    return entry.state is State.SKIPPED and not entry.final and sale.last_modified > entry.updated_at


def _too_old(sale: Sale, now: datetime, settings: RunSettings) -> bool:
    invoiced = sale.invoice_date or sale.order_date
    return now - invoiced > timedelta(days=settings.max_invoice_age_days)


def _act(sale: Sale, decision: Decision, ledger: Ledger, warehouse: Warehouse, report: RunReport) -> None:
    so = sale.so_number
    if decision.route is Route.SKIP:
        ledger.move(so, State.SKIPPED, decision.summary, final=decision.final)
        report.note(so, "skipped", decision.summary)
    elif decision.route is Route.HOLD:
        ledger.move(so, State.HELD, decision.summary)
        report.note(so, "held", decision.summary)
    elif problems := warehouse.prepare(sale).problems:
        # The warehouse would refuse it, so a person fixes it at source first.
        why = " ".join(problems)
        ledger.move(so, State.HELD, why)
        report.note(so, "held", why)
    else:
        result = push(sale, ledger, warehouse)
        action = result.state.value.lower() if result.state else "not sent"
        report.note(so, action, result.reason)
        if result.stop_run:
            report.stopped = result.reason
