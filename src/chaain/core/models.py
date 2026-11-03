"""The sale: the one shape every stage works with.

Connectors translate their system's payload into these types, so core logic
never sees a Cin7-style field name. Records are frozen: once built, a sale
can't be changed by accident, only replaced with a new copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class SaleStatus(str, Enum):
    """Where the sale is in the stock system's workflow.

    The stock system tracks picking, packing, shipping and invoicing
    separately; the connector collapses them into this one value, led by the
    invoice status, which is what the live filter checks.
    """

    DRAFT = "Draft"
    PICKED = "Picked"
    PACKED = "Packed"
    INVOICED = "Invoiced"
    # After invoicing. If the order already went to the warehouse, a person
    # has to fix it there: nothing flows back automatically.
    CREDITED = "Credited"
    VOIDED = "Voided"


class SourceChannel(str, Enum):
    """Where the order came from. The stock system leaves this blank for
    wholesale; the connector maps blank to WHOLESALE."""

    WHOLESALE = "Wholesale"
    SHOPIFY = "Shopify"


def _require_aware(name: str, value: datetime | None) -> None:
    # Naive datetimes can't be compared safely across a lookback window.
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class Address:
    line1: str
    line2: str
    city: str
    state: str
    postcode: str


@dataclass(frozen=True)
class Contact:
    """A contact on the customer record, read by the connector with the sale.

    The delivery contact is chosen at push time: a "Delivery" job title wins,
    because the record's default contact is often the buyer or accounts.
    """

    name: str
    job_title: str
    phone: str
    mobile: str
    is_default: bool


@dataclass(frozen=True)
class SaleLine:
    """One product on a sale, from one pick location.

    A line picked from two places is split by the connector into two lines,
    so each line has exactly one pick location. Lines from every fulfilment
    are included, not just the first.
    """

    sku: str
    product_name: str
    vintage: int | None  # part of the SKU; None for non-vintage ("NV")
    qty: int  # single bottles, never cases
    price: Decimal  # sent to the warehouse; zero for samples
    pick_location: str
    fulfilment: int = 1  # which fulfilment the line was picked in

    def __post_init__(self) -> None:
        if not self.sku:
            raise ValueError("sku is required")
        if isinstance(self.qty, bool) or not isinstance(self.qty, int) or self.qty <= 0:
            raise ValueError(f"qty must be a positive whole number of bottles, got {self.qty!r}")
        if self.price < 0:
            raise ValueError("price can't be negative")
        if self.fulfilment < 1:
            raise ValueError("fulfilment numbers start at 1")


@dataclass(frozen=True)
class Sale:
    """A sale as read from the stock system.

    `note` is the stock system's sale-level note. It starts as a copy of the
    customer's standing comments and can be edited per order, so it carries
    delivery instructions, the delivery window (as HH:MM-HH:MM text) and
    markers such as #NOVISION. `memo` is the order's second free-text field,
    which can also carry #NOVISION.
    """

    id: str  # stock-system GUID
    so_number: str  # the doc ref the warehouse sees
    status: SaleStatus
    source_channel: SourceChannel
    customer_id: str
    customer_name: str
    contact_name: str  # sale header contact, copied from the default at creation
    phone: str  # as entered in the stock system; formatted at push time
    contacts: tuple[Contact, ...]  # the customer record's contacts
    shipping_address: Address
    customer_reference: str
    header_location: str
    note: str
    memo: str
    order_date: datetime
    invoice_date: datetime | None  # None until invoiced
    last_modified: datetime
    lines: tuple[SaleLine, ...]

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("id is required")
        if not self.so_number:
            raise ValueError("so_number is required")
        if not isinstance(self.status, SaleStatus):
            raise ValueError(f"unknown status {self.status!r}")
        if not isinstance(self.source_channel, SourceChannel):
            raise ValueError(f"unknown source channel {self.source_channel!r}")
        if not isinstance(self.lines, tuple) or not isinstance(self.contacts, tuple):
            raise ValueError("lines and contacts must be tuples, so the sale stays unchangeable")
        _require_aware("order_date", self.order_date)
        _require_aware("invoice_date", self.invoice_date)
        _require_aware("last_modified", self.last_modified)

    @property
    def pick_locations(self) -> frozenset[str]:
        """Every location stock was picked from. More than one means mixed."""
        return frozenset(line.pick_location for line in self.lines)

    @property
    def fulfilments(self) -> frozenset[int]:
        return frozenset(line.fulfilment for line in self.lines)

    @property
    def total_bottles(self) -> int:
        return sum(line.qty for line in self.lines)
