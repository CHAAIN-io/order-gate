"""What core logic needs from a warehouse, and nothing more.

Two steps, kept apart on purpose:
- `prepare` turns a sale into the warehouse's format and lists anything that
  format can't accept (no phone, too many lines). It sends nothing, so the
  gate can use it to hold orders the warehouse would refuse.
- `send` makes exactly one attempt and reports what happened at the
  transport level. Deciding what that *means* (sent, refused, unclear) is the
  push workflow's job, not the connector's.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from chaain.core.models import Sale


@dataclass(frozen=True)
class PreparedOrder:
    doc_ref: str  # the reference the warehouse files the order under (the SO number)
    payload: bytes
    problems: tuple[str, ...]  # reasons the warehouse would refuse it; empty means sendable
    flags: dict = field(default_factory=dict)  # what was adjusted, for the log


class Transport(str, Enum):
    RESPONSE = "response"  # an HTTP response came back
    TIMEOUT = "timeout"  # we stopped waiting; it may have arrived
    DROPPED = "dropped"  # the connection broke mid-request; it may have arrived
    CONNECT_FAILED = "connect_failed"  # never connected; it cannot have arrived


@dataclass(frozen=True)
class SendResult:
    transport: Transport
    status: int | None = None
    body: str = ""
    elapsed_seconds: float = 0.0


class Warehouse(ABC):
    @abstractmethod
    def prepare(self, sale: Sale) -> PreparedOrder:
        """Build the order in the warehouse's format. Pure: sends nothing."""

    @abstractmethod
    def send(self, order: PreparedOrder) -> SendResult:
        """One attempt, never retried here."""

    def lookup(self, doc_ref: str) -> bool | None:
        """Whether the warehouse has an order with this doc ref.

        None means the warehouse can't be asked, which is the real situation
        today, so a person has to check. Connectors that can look it up
        override this.
        """
        return None
