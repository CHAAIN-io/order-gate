"""The resolver: finds what the system can't settle alone and asks a person.

It runs on its own schedule, all day, every day, not only when orders
arrive. The live workflow checked for stuck orders only when a poll found
recent sales, so an order stuck on a Friday evening could wait all weekend.

Each run:
1. A PENDING row claimed longer ago than the stale limit, with no outcome
   recorded, becomes CHECK. It may or may not be at the warehouse.
2. If the warehouse can be asked by doc ref, each CHECK is looked up.
   Found means SENT: nothing is resent, so that's safe to automate. Not
   found stays with a person, with the lookup as evidence: the order may
   just not be visible yet, and a wrong "no" leads to a duplicate delivery.
3. Anything newly needing a person (CHECK, HELD, REJECTED) goes into one
   alert. Each item is alerted once per state, and the alert is logged on
   the row. If the alert fails, it's tried again next run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from chaain.connectors.notifier.interface import Notifier, NotifyError
from chaain.connectors.warehouse.interface import Warehouse
from chaain.core.ledger import Entry, Event, Ledger, LedgerError, State
from chaain.workflows.order_push.scheduler import Schedule, Window

ACTOR = "resolver"
NEEDS_PERSON = (State.CHECK, State.HELD, State.REJECTED)
ALERTED = "alert sent"
NOT_FOUND = "warehouse lookup found nothing yet; a person must confirm before any resend"


@dataclass(frozen=True)
class ResolverSettings:
    every_minutes: int = 15
    stale_pending_minutes: int = 30
    lookup_enabled: bool = False

    @classmethod
    def from_config(cls, settings: dict) -> "ResolverSettings":
        r = settings["resolver"]
        return cls(r["every_minutes"], r["stale_pending_minutes"], settings["warehouse"]["lookup_enabled"])


def resolver_schedule(settings: ResolverSettings, tz: str = "Europe/London") -> Schedule:
    """Every N minutes, round the clock, weekends included."""
    return Schedule((Window(time(0, 0), time(23, 59), settings.every_minutes),), ZoneInfo(tz))


@dataclass
class ResolverReport:
    at: datetime
    made_check: list[str] = field(default_factory=list)  # stale PENDING -> CHECK
    confirmed_sent: list[str] = field(default_factory=list)  # found by lookup
    not_found: list[str] = field(default_factory=list)  # lookup said no: still a person's call
    alerted: list[str] = field(default_factory=list)
    alert_failed: str | None = None


def resolve(
    now: datetime, ledger: Ledger, warehouse: Warehouse, notifier: Notifier,
    settings: ResolverSettings = ResolverSettings(),
) -> ResolverReport:
    report = ResolverReport(at=now)
    _stale_pending(now, ledger, settings, report)
    if settings.lookup_enabled:
        _look_up(ledger, warehouse, report)
    _alert(ledger, notifier, report)
    return report


def _stale_pending(now: datetime, ledger: Ledger, settings: ResolverSettings, report: ResolverReport) -> None:
    limit = timedelta(minutes=settings.stale_pending_minutes)
    for entry in ledger.in_state(State.PENDING):
        age = now - entry.updated_at
        if age < limit:
            continue  # a send may still be in progress
        minutes = int(age.total_seconds() // 60)
        try:
            ledger.move(
                entry.so_number, State.CHECK,
                f"claimed {minutes} minutes ago with no outcome recorded; it may have arrived",
                ACTOR, details={"claimed_at": entry.updated_at.isoformat()},
            )
        except LedgerError:
            continue  # the push recorded its outcome in the meantime
        report.made_check.append(entry.so_number)


def _look_up(ledger: Ledger, warehouse: Warehouse, report: ResolverReport) -> None:
    for entry in ledger.in_state(State.CHECK):
        try:
            found = warehouse.lookup(entry.so_number)
        except Exception:
            found = None  # can't tell: leave it with a person
        if found is True:
            try:
                ledger.move(entry.so_number, State.SENT, "found at the warehouse by doc ref", ACTOR)
            except LedgerError:
                continue
            report.confirmed_sent.append(entry.so_number)
        elif found is False:
            report.not_found.append(entry.so_number)
            if not _noted_since_change(ledger.history(entry.so_number), NOT_FOUND):
                ledger.annotate(entry.so_number, NOT_FOUND, ACTOR, details={"lookup": "not found"})


def _alert(ledger: Ledger, notifier: Notifier, report: ResolverReport) -> None:
    new = [e for e in ledger.in_state(*NEEDS_PERSON) if not _noted_since_change(ledger.history(e.so_number), ALERTED)]
    if not new:
        return
    subject = f"{len(new)} order{'s' if len(new) != 1 else ''} need{'s' if len(new) == 1 else ''} a person"
    body = "\n".join(_line(e) for e in new) + "\n\nOpen the team queue to act on them."
    try:
        notifier.notify(subject, body)
    except NotifyError as e:
        report.alert_failed = str(e)  # not logged as alerted, so the next run tries again
        return
    for entry in new:
        ledger.annotate(entry.so_number, ALERTED, ACTOR)
        report.alerted.append(entry.so_number)


def _line(entry: Entry) -> str:
    what = {
        State.CHECK: "may or may not be at the warehouse",
        State.HELD: "held for a decision",
        State.REJECTED: "refused by the warehouse",
    }[entry.state]
    return f"- {entry.so_number}: {what}. {entry.reason}"


def _noted_since_change(history: list[Event], reason: str) -> bool:
    """Whether a note with this reason was logged after the latest state change."""
    for event in reversed(history):
        if not event.is_note:
            return False
        if event.reason == reason:
            return True
    return False
