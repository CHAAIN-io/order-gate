"""A warehouse you can tell how to answer, which remembers what it received.

The point of `received` is that it can differ from what the sender was told.
A timeout can still mean the order arrived and is being picked; the mock
records it as received so tests can prove we never send it twice.

Like the live warehouse, it rejects XML that isn't well-formed or doesn't
start with the exact declaration it expects.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import date

from chaain.connectors.warehouse import edi_xml
from chaain.connectors.warehouse.interface import (
    PreparedOrder, SendResult, Transport, Warehouse,
)
from chaain.core.models import Sale


@dataclass(frozen=True)
class Behaviour:
    """How the mock answers one send."""

    transport: Transport
    status: int | None = None
    body: str = ""
    received: bool = False  # whether the order really reached the warehouse
    delay_seconds: float = 0.5

    # The cases the push has to handle.
    @classmethod
    def accept(cls):
        return cls(Transport.RESPONSE, 200, "Order saved", received=True)

    @classmethod
    def reject(cls, message="Validation failed: DeliveryPostcode invalid"):
        return cls(Transport.RESPONSE, 400, message)

    @classmethod
    def auth_fail(cls):
        return cls(Transport.RESPONSE, 401, "Unauthorised")

    @classmethod
    def server_error(cls, received=False):
        return cls(Transport.RESPONSE, 500, "Internal Server Error", received=received)

    @classmethod
    def garbled(cls, received=True):
        return cls(Transport.RESPONSE, 200, "<html>Gateway</html>", received=received)

    @classmethod
    def timeout(cls, received=True):
        return cls(Transport.TIMEOUT, received=received, delay_seconds=float("inf"))

    @classmethod
    def dropped(cls, received=True):
        return cls(Transport.DROPPED, received=received)

    @classmethod
    def connect_fail(cls):
        return cls(Transport.CONNECT_FAILED, delay_seconds=0.0)

    @classmethod
    def slow(cls, seconds: float):
        """Accepted after `seconds`. Past the timeout, the sender sees a timeout."""
        return cls(Transport.RESPONSE, 200, "Order saved", received=True, delay_seconds=seconds)


class MockWarehouse(Warehouse):
    def __init__(
        self,
        settings: edi_xml.EdiSettings = edi_xml.EdiSettings(),
        script: list[Behaviour] | tuple[Behaviour, ...] = (),
        default: Behaviour = Behaviour.accept(),
        response_timeout_seconds: float = 60,
        lookup_enabled: bool = False,
        today: date | None = None,
    ) -> None:
        self.settings = settings
        self.script = list(script)  # answers used in order, then `default`
        self.default = default
        self.response_timeout_seconds = response_timeout_seconds
        self.lookup_enabled = lookup_enabled
        self.today = today or date.today()
        self.attempts: list[str] = []  # every doc ref we were asked to send
        self.received: Counter[str] = Counter()  # what actually arrived

    def prepare(self, sale: Sale) -> PreparedOrder:
        return edi_xml.build(sale, self.settings, self.today)

    def send(self, order: PreparedOrder) -> SendResult:
        self.attempts.append(order.doc_ref)
        behaviour = self.script.pop(0) if self.script else self.default

        if behaviour.transport is Transport.CONNECT_FAILED:
            return SendResult(Transport.CONNECT_FAILED)

        invalid = _validate(order.payload)
        if invalid and behaviour.transport is Transport.RESPONSE:
            return SendResult(Transport.RESPONSE, 400, invalid, 0.2)

        if behaviour.received:
            self.received[order.doc_ref] += 1
        if behaviour.delay_seconds > self.response_timeout_seconds:
            return SendResult(Transport.TIMEOUT, elapsed_seconds=self.response_timeout_seconds)
        if behaviour.transport is not Transport.RESPONSE:
            return SendResult(behaviour.transport, elapsed_seconds=behaviour.delay_seconds)
        return SendResult(Transport.RESPONSE, behaviour.status, behaviour.body, behaviour.delay_seconds)

    def lookup(self, doc_ref: str) -> bool | None:
        if not self.lookup_enabled:
            return None
        return self.received[doc_ref] > 0


def _validate(payload: bytes) -> str | None:
    """What the live warehouse checks before it looks at the order."""
    if not payload.startswith(edi_xml.DECLARATION.encode()):
        return "XML declaration must be exactly " + edi_xml.DECLARATION
    try:
        ET.fromstring(payload)
    except ET.ParseError as e:
        return f"XML not well-formed: {e}"
    return None
