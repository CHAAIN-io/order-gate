"""Synthetic sales, with a named scenario for every awkward case we know of.

Deterministic: the same seed, `now` and sequence of calls give the same sales
every run, so a failing test fails every time.

All data is made up. Phone numbers come from the 07700 900xxx range Ofcom
reserves for drama; venues and wines are obvious placeholders.

Usage:
    gen = SaleGenerator(seed=1)
    gen.make("site_b_pick")                       # a named scenario
    gen.make("standard", note="Ring on arrival")  # scenario plus overrides
    gen.day(n=20)                                 # a realistic mixed day
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from chaain.core.models import Address, Sale, SaleLine, SaleStatus, SourceChannel

WAREHOUSE = "Warehouse-3PL"
SITE_B = "Site-B"
# The warehouse set up as a customer, for stock corrections. Never a real pick.
INTERNAL_CUSTOMER_ID = "00000000-0000-4000-8000-000000000001"
DEFAULT_NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)

# sku, product name, vintage (None = non-vintage), price per bottle
CATALOGUE = (
    ("CW-R001", "Demo Estate Rouge", 2021, "9.50"),
    ("CW-R002", "Demo Estate Rouge", 2022, "9.75"),
    ("CW-R003", "Sample Hill Syrah", 2020, "12.40"),
    ("CW-W001", "Demo Estate Blanc", 2023, "8.90"),
    ("CW-W002", "Test Valley Riesling", 2022, "11.20"),
    ("CW-P001", "Placeholder Rose", 2024, "7.80"),
    ("CW-S001", "Mock Brut", None, "14.00"),
    ("CW-S002", "Fixture Cremant", None, "10.60"),
)

# Standing customer comments, copied into each sale's note. Most carry a
# delivery window in the strict HH:MM-HH:MM form the push expects.
COMMENTS = (
    "Deliver to side entrance. 09:00-13:00",
    "Ring bell, cellar hatch at rear. 10:00-14:00",
    "Reception signs for deliveries. 08:00-12:00",
    "Goods in via loading bay. 07:00-11:00",
    "[No Delivery Instructions]",
)

TOWNS = (("EC1A 9ZZ", "London"), ("SE1 9ZZ", "London"), ("N1 9ZZ", "London"), ("BS1 9ZZ", "Bristol"))


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    kind: str  # "restaurant" orders in sixes; "shop" takes 2 or 3 of several wines
    contact_name: str
    phone: str
    address: Address
    comments: str


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    build: Callable[["SaleGenerator", Sale], Sale]


SCENARIOS: dict[str, Scenario] = {}


def scenario(name: str, description: str):
    def register(build):
        SCENARIOS[name] = Scenario(name, description, build)
        return build
    return register


class SaleGenerator:
    def __init__(self, seed: int = 0, now: datetime = DEFAULT_NOW, customers: int = 12) -> None:
        self.rng = random.Random(seed)
        self.now = now
        self._next_so = 10001
        self.customers = tuple(self._customer(i) for i in range(1, customers + 1))

    # ---------- public ----------

    def make(self, scenario: str = "standard", **overrides) -> Sale:
        """One sale for a named scenario, with any fields overridden."""
        if scenario not in SCENARIOS:
            raise KeyError(f"unknown scenario {scenario!r}; known: {', '.join(sorted(SCENARIOS))}")
        sale = SCENARIOS[scenario].build(self, self._base())
        return replace(sale, **overrides) if overrides else sale

    def day(self, n: int = 20, mix: dict[str, int] | None = None) -> list[Sale]:
        """A day's sales: the scenarios in `mix`, topped up with standard ones."""
        mix = DEFAULT_DAY_MIX if mix is None else mix
        wanted = sum(mix.values())
        if wanted > n:
            raise ValueError(f"mix asks for {wanted} sales but n is {n}")
        names = [name for name, count in mix.items() for _ in range(count)]
        names += ["standard"] * (n - wanted)
        self.rng.shuffle(names)
        return [self.make(name) for name in names]

    # ---------- building blocks ----------

    def guid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def phone(self) -> str:
        # Mixed formats on purpose: the push has to normalise them.
        digits = f"{self.rng.randint(0, 999):03d}"
        return self.rng.choice((f"07700 900{digits}", f"07700900{digits}", f"+44 7700 900{digits}"))

    def line(self, product: tuple, qty: int, location: str = WAREHOUSE) -> SaleLine:
        sku, name, vintage, price = product
        return SaleLine(sku, name, vintage, qty, Decimal(price), location)

    def lines_for(self, customer: Customer) -> tuple[SaleLine, ...]:
        if customer.kind == "restaurant":
            products = self.rng.sample(CATALOGUE, self.rng.randint(2, 4))
            lines = [self.line(p, self.rng.choice((6, 12, 18))) for p in products]
        else:
            products = self.rng.sample(CATALOGUE, self.rng.randint(4, 6))
            lines = [self.line(p, self.rng.choice((2, 3))) for p in products]
        # Keep ordinary sales above the smallest regional minimum (24 bottles).
        while sum(l.qty for l in lines) < 24:
            lines[0] = replace(lines[0], qty=lines[0].qty + 6)
        return tuple(lines)

    def _customer(self, i: int) -> Customer:
        postcode, town = self.rng.choice(TOWNS)
        return Customer(
            id=self.guid(),
            name=f"Demo Venue {i:02d}",
            kind=self.rng.choice(("restaurant", "restaurant", "shop")),
            contact_name=self.rng.choice(("Duty Manager", "Head Chef", "Bar Manager", "Owner")),
            phone=self.phone(),
            address=Address(f"{i} Demo Street", "", town, "", postcode),
            comments=self.rng.choice(COMMENTS),
        )

    def _base(self, customer: Customer | None = None) -> Sale:
        """A plain invoiced wholesale sale: the 'standard' scenario."""
        customer = customer or self.rng.choice(self.customers)
        invoiced = self.now - timedelta(minutes=self.rng.randint(30, 180))
        so_number = f"SO-{self._next_so:05d}"
        self._next_so += 1
        return Sale(
            id=self.guid(),
            so_number=so_number,
            status=SaleStatus.INVOICED,
            source_channel=SourceChannel.WHOLESALE,
            customer_id=customer.id,
            customer_name=customer.name,
            contact_name=customer.contact_name,
            phone=customer.phone,
            shipping_address=customer.address,
            customer_reference="",
            header_location=WAREHOUSE,
            note=customer.comments,
            memo="",
            order_date=invoiced - timedelta(days=self.rng.randint(0, 2), hours=2),
            invoice_date=invoiced,
            last_modified=invoiced + timedelta(minutes=self.rng.randint(1, 10)),
            lines=self.lines_for(customer),
        )


def _at(sale: Sale, location: str, every: int = 1) -> tuple[SaleLine, ...]:
    """Move every `every`-th line (starting with the first) to `location`."""
    return tuple(
        replace(line, pick_location=location) if i % every == 0 else line
        for i, line in enumerate(sale.lines)
    )


# ---------- scenarios ----------
# Each one changes only what makes it awkward, so a test failing on it points
# at exactly one rule.

@scenario("standard", "Plain wholesale order, invoiced, picked at Warehouse-3PL. Should go automatically.")
def _standard(gen, sale):
    return sale


@scenario("not_invoiced", "Still at Draft, Picked or Packed. Not ready to send.")
def _not_invoiced(gen, sale):
    status = gen.rng.choice((SaleStatus.DRAFT, SaleStatus.PICKED, SaleStatus.PACKED))
    return replace(sale, status=status, invoice_date=None)


@scenario("shopify", "Shopify order. Handled by hand, never pushed.")
def _shopify(gen, sale):
    return replace(sale, source_channel=SourceChannel.SHOPIFY, customer_name="Shopify Customer")


@scenario("site_b_header", "Order header says Site-B, and it was picked there.")
def _site_b_header(gen, sale):
    return replace(sale, header_location=SITE_B, lines=_at(sale, SITE_B))


@scenario("site_b_pick", "Header says Warehouse-3PL but every line was picked at Site-B (past incident).")
def _site_b_pick(gen, sale):
    return replace(sale, lines=_at(sale, SITE_B))


@scenario("mixed_pick", "Some lines picked at Warehouse-3PL, some at Site-B.")
def _mixed_pick(gen, sale):
    if len(sale.lines) < 2:
        sale = replace(sale, lines=sale.lines + (gen.line(CATALOGUE[3], 6),))
    return replace(sale, lines=_at(sale, SITE_B, every=2))


@scenario("internal_customer", "Warehouse-3PL itself is the customer: a stock correction, not a delivery.")
def _internal_customer(gen, sale):
    return replace(sale, customer_id=INTERNAL_CUSTOMER_ID, customer_name="Warehouse-3PL (internal)")


@scenario("novision_note", "#NOVISION in the note: a re-bill for goods already delivered.")
def _novision_note(gen, sale):
    return replace(sale, note=f"#NOVISION re-bill for extras already delivered. {sale.note}")


@scenario("novision_memo", "#NOVISION in the memo rather than the note.")
def _novision_memo(gen, sale):
    return replace(sale, memo="#NOVISION")


@scenario("reserve_note", "RESERVE FOR note: stock set aside for another customer.")
def _reserve_note(gen, sale):
    return replace(sale, note=f"RESERVE FOR Demo Venue 99, do not rotate. {sale.note}")


@scenario("time_in_instructions", "A delivery time written as free text, not as a window. The warehouse forbids times in instructions.")
def _time_in_instructions(gen, sale):
    return replace(sale, note="Deliver before 10am please, kitchen closed after.")


@scenario("no_window", "Instructions with no delivery window. The default window applies.")
def _no_window(gen, sale):
    return replace(sale, note="Leave with bar staff.")


@scenario("long_instructions", "Instructions longer than the warehouse's 120-character field.")
def _long_instructions(gen, sale):
    note = (
        "Use the service entrance on the side street, not the main door. Ask for the "
        "cellar key at the bar and stack cases by the cold room door, not in the corridor. "
        "09:00-13:00"
    )
    return replace(sale, note=note)


@scenario("no_phone", "No phone number on the sale. The warehouse needs one for the driver.")
def _no_phone(gen, sale):
    return replace(sale, phone="")


@scenario("missing_town", "Delivery address with no town. The warehouse needs a valid post town.")
def _missing_town(gen, sale):
    return replace(sale, shipping_address=replace(sale.shipping_address, city="", state=""))


@scenario("too_many_lines", "51 lines. The warehouse accepts at most 50 per order.")
def _too_many_lines(gen, sale):
    lines = tuple(
        gen.line((f"CW-X{i:03d}", f"Sample Bin {i}", 2022, "9.00"), 6) for i in range(1, 52)
    )
    return replace(sale, lines=lines)


@scenario("below_minimum", "12 bottles: below the smallest regional minimum.")
def _below_minimum(gen, sale):
    return replace(sale, lines=(gen.line(CATALOGUE[0], 6), gen.line(CATALOGUE[3], 6)))


@scenario("old_invoice", "Invoiced 10 days ago, pulled back into the window by a payment sync.")
def _old_invoice(gen, sale):
    invoiced = gen.now - timedelta(days=10)
    return replace(
        sale, order_date=invoiced - timedelta(hours=2), invoice_date=invoiced,
        last_modified=gen.now - timedelta(minutes=gen.rng.randint(5, 60)),
    )


@scenario("new_customer", "First order from a customer the system has never seen.")
def _new_customer(gen, sale):
    customer = gen._customer(len(gen.customers) + 1)
    return replace(
        sale, customer_id=customer.id, customer_name=customer.name,
        contact_name=customer.contact_name, phone=customer.phone,
        shipping_address=customer.address, note=customer.comments,
    )


# A day that touches the main rules, topped up with standard sales.
DEFAULT_DAY_MIX = {
    "not_invoiced": 2,
    "shopify": 1,
    "site_b_pick": 1,
    "mixed_pick": 1,
    "internal_customer": 1,
    "novision_note": 1,
    "reserve_note": 1,
    "time_in_instructions": 1,
    "below_minimum": 1,
    "new_customer": 1,
}
