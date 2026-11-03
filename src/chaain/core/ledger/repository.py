"""The ledger as the rest of the system sees it.

Callers ask in plain terms ("claim this sale", "mark it sent") and never
write SQL, so SQLite can be swapped for Postgres by writing one new class.

Every row is matched on BOTH the stock-system GUID and the SO number: the SO
number is the doc ref the warehouse sees, so either match blocks a send. That
is what stops a hand-entered row with a truncated GUID from being missed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from chaain.core.ledger.states import State


class LedgerError(Exception):
    pass


class DuplicateEntry(LedgerError):
    """A row already exists for this GUID or SO number."""


class InvalidTransition(LedgerError):
    """The state machine doesn't allow this move."""


class NotFound(LedgerError):
    pass


class ConcurrentChange(LedgerError):
    """Someone else moved the row between our read and our write."""


@dataclass(frozen=True)
class Entry:
    so_number: str
    guid: str | None  # None or partial for some hand-entered rows
    customer_id: str | None
    state: State
    final: bool  # only meaningful for SKIPPED
    source: str  # "system" or "manual"
    reason: str  # the latest reason
    created_at: datetime
    updated_at: datetime
    fingerprint: str | None  # the sale as claimed, to spot changes after sending


@dataclass(frozen=True)
class Event:
    from_state: State | None
    to_state: State
    reason: str
    actor: str
    at: datetime
    details: dict | None


def normalise_so(so_number: str) -> str:
    return so_number.strip().upper()


def normalise_guid(guid: str | None) -> str | None:
    guid = (guid or "").strip().lower()
    return guid or None


class Ledger(ABC):
    @abstractmethod
    def find(self, guid: str | None = None, so_number: str | None = None) -> Entry | None:
        """The row matching the GUID or the SO number, GUID match first."""

    @abstractmethod
    def open(self, so_number: str, guid: str, customer_id: str, reason: str) -> Entry:
        """Record a newly seen sale as CANDIDATE. DuplicateEntry if either key exists."""

    @abstractmethod
    def move(
        self, so_number: str, to: State, reason: str, actor: str = "system", *,
        final: bool = False, fingerprint: str | None = None, details: dict | None = None,
    ) -> Entry:
        """Change state and log why, in one transaction: both happen or neither does."""

    def claim(self, so_number: str, reason: str, fingerprint: str | None = None) -> Entry:
        """Write PENDING before sending. If this raises, the caller must not send."""
        return self.move(so_number, State.PENDING, reason, fingerprint=fingerprint)

    @abstractmethod
    def record_manual(self, so_number: str, guid: str | None, actor: str, reason: str) -> Entry:
        """An order someone entered at the warehouse by hand: straight to SENT."""

    @abstractmethod
    def history(self, so_number: str) -> list[Event]:
        """Every state change for a row, oldest first."""

    @abstractmethod
    def in_state(self, *states: State) -> list[Entry]:
        """Rows currently in any of these states, oldest change first."""
