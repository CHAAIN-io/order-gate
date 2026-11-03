"""Synthetic sales, with a named scenario for every awkward case we know of.

Deterministic: the same seed, `now` and sequence of calls give the same sales
every run, so a failing test fails every time.

All data is made up. Phone numbers come from the ranges Ofcom reserves for
drama (07700 900xxx, 020 7946 0xxx); venues and wines are obvious placeholders.
Times are UK local, so sales either side of a clock change carry the right
offset.

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
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from chaain.core.models import Address, Contact, Sale, SaleLine, SaleStatus, SourceChannel

UK = ZoneInfo("Europe/London")
WAREHOUSE = "Warehouse-3PL"
SITE_B = "Site-B"
# The warehouse set up as a customer, for stock corrections. Never a real pick.
INTERNAL_CUSTOMER_ID = "00000000-0000-4000-8000-000000000001"
DEFAULT_NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UK)  # GMT; clocks went back on 25 Oct


def sku(supplier: str, wine: int, vintage: int | None, kind: str) -> str:
    """The client's SKU convention: supplier (3 letters), wine (2 digits),
    vintage (2 digits, or NV), type suffix. E.g. ABC0623RW."""
    year = "NV" if vintage is None else f"{vintage % 100:02d}"
    return f"{supplier}{wine:02d}{year}{kind}"


# (sku, product name, vintage, price per bottle). Only the RW suffix is
# confirmed; the other type suffixes are assumed.
CATALOGUE = (
    (sku("DEM", 1, 2021, "RW"), "Demo Estate Rouge 2021", 2021, "9.50"),
    (sku("DEM", 1, 2022, "RW"), "Demo Estate Rouge 2022", 2022, "9.75"),
    (sku("SMP", 3, 2020, "RW"), "Sample Hill Syrah 2020", 2020, "12.40"),
    (sku("DEM", 2, 2023, "WW"), "Demo Estate Blanc 2023", 2023, "8.90"),
    (sku("TST", 1, 2022, "WW"), "Test Valley Riesling 2022", 2022, "11.20"),
    (sku("PLC", 1, 2024, "RS"), "Placeholder Rose 2024", 2024, "7.80"),
    (sku("MCK", 1, None, "SP"), "Mock Brut NV", None, "14.00"),
    (sku("FXT", 1, None, "SP"), "Fixture Cremant NV", None, "10.60"),
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

# Customers are spread across these in turn, so every pool has both regions.
TOWNS = (
    ("EC1A 9ZZ", "London", "London"),
    ("EH1 9ZZ", "Edinburgh", "Scotland"),
    ("SE1 9ZZ", "London", "London"),
    ("G1 9ZZ", "Glasgow", "Scotland"),
    ("N1 9ZZ", "London", "London"),
)
# Typical smallest order per region, so ordinary sales clear the (inferred)
# regional minimums. The gate's real thresholds live in config.
TYPICAL_MINIMUM = {"London": 24, "Scotland": 120}


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    kind: str  # "restaurant" or "shop"; order sizes per kind are assumed
    region: str
    contacts: tuple[Contact, ...]
    address: Address
    comments: str

    @property
    def default_contact(self) -> Contact:
        return next(c for c in self.contacts if c.is_default)


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

    def customer_in(self, region: str) -> Customer:
        return self.rng.choice([c for c in self.customers if c.region == region])

    # ---------- building blocks ----------

    def guid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def mobile(self) -> str:
        # Mixed formats on purpose: the push has to normalise them.
        digits = f"{self.rng.randint(0, 999):03d}"
        return self.rng.choice((f"07700 900{digits}", f"07700900{digits}", f"+44 7700 900{digits}"))

    def landline(self) -> str:
        return f"020 7946 0{self.rng.randint(0, 999):03d}"

    def line(self, product: tuple, qty: int, location: str = WAREHOUSE, fulfilment: int = 1) -> SaleLine:
        code, name, vintage, price = product
        return SaleLine(code, name, vintage, qty, Decimal(price), location, fulfilment)

    def lines_for(self, customer: Customer) -> tuple[SaleLine, ...]:
        if customer.kind == "restaurant":
            products = self.rng.sample(CATALOGUE, self.rng.randint(2, 4))
            lines = [self.line(p, self.rng.choice((6, 12, 18))) for p in products]
        else:
            products = self.rng.sample(CATALOGUE, self.rng.randint(4, 6))
            lines = [self.line(p, self.rng.choice((2, 3))) for p in products]
        # Top up in sixes, line by line, until the order clears its region's minimum.
        i = 0
        while sum(l.qty for l in lines) < TYPICAL_MINIMUM[customer.region]:
            lines[i % len(lines)] = replace(lines[i % len(lines)], qty=lines[i % len(lines)].qty + 6)
            i += 1
        return tuple(lines)

    def _customer(self, i: int) -> Customer:
        postcode, town, region = TOWNS[(i - 1) % len(TOWNS)]
        role = self.rng.choice(("Duty Manager", "Head Chef", "Bar Manager", "Owner"))
        contacts = [Contact(role, role, self.landline(), self.mobile(), True)]
        if self.rng.random() < 0.5:
            contacts.append(Contact("Goods In", "Delivery", "", self.mobile(), False))
        return Customer(
            id=self.guid(),
            name=f"Demo Venue {i:02d}",
            kind=self.rng.choice(("restaurant", "restaurant", "shop")),
            region=region,
            contacts=tuple(contacts),
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
            **_customer_fields(customer),
            customer_reference="",
            header_location=WAREHOUSE,
            memo="",
            order_date=invoiced - timedelta(days=self.rng.randint(0, 2), hours=2),
            invoice_date=invoiced,
            last_modified=invoiced + timedelta(minutes=self.rng.randint(1, 10)),
            lines=self.lines_for(customer),
        )


def _customer_fields(customer: Customer) -> dict:
    """The sale fields that come from the customer record. The header contact
    is copied from the default contact when the sale is created."""
    default = customer.default_contact
    return dict(
        customer_id=customer.id,
        customer_name=customer.name,
        contact_name=default.name,
        phone=default.phone,
        contacts=customer.contacts,
        shipping_address=customer.address,
        note=customer.comments,
        standing_note=customer.comments,
    )


def _at(sale: Sale, location: str, every: int = 1) -> tuple[SaleLine, ...]:
    """Move every `every`-th line (starting with the first) to `location`."""
    return tuple(
        replace(line, pick_location=location) if i % every == 0 else line
        for i, line in enumerate(sale.lines)
    )


def _at_least_two_lines(gen: SaleGenerator, sale: Sale) -> Sale:
    if len(sale.lines) >= 2:
        return sale
    return replace(sale, lines=sale.lines + (gen.line(CATALOGUE[3], 6),))


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


@scenario("credited", "Credited after invoicing. If it was already sent, a person must fix it at the warehouse.")
def _credited(gen, sale):
    return replace(sale, status=SaleStatus.CREDITED, last_modified=gen.now - timedelta(minutes=5))


@scenario("shopify", "Shopify order picked at Site-B, the default. Not for the warehouse.")
def _shopify(gen, sale):
    return replace(
        sale, source_channel=SourceChannel.SHOPIFY, customer_name="Shopify Customer",
        header_location=SITE_B, lines=_at(sale, SITE_B),
    )


@scenario("shopify_warehouse_pick", "Shopify order switched to Warehouse-3PL stock. Excluded by channel, so it needs a person to send it (past discrepancy).")
def _shopify_warehouse_pick(gen, sale):
    return replace(sale, source_channel=SourceChannel.SHOPIFY, customer_name="Shopify Customer")


@scenario("site_b_header", "Order header says Site-B, and it was picked there.")
def _site_b_header(gen, sale):
    return replace(sale, header_location=SITE_B, lines=_at(sale, SITE_B))


@scenario("site_b_pick", "Header says Warehouse-3PL but every line was picked at Site-B (past incident).")
def _site_b_pick(gen, sale):
    return replace(sale, lines=_at(sale, SITE_B))


@scenario("mixed_pick", "Some lines picked at Warehouse-3PL, some at Site-B.")
def _mixed_pick(gen, sale):
    sale = _at_least_two_lines(gen, sale)
    return replace(sale, lines=_at(sale, SITE_B, every=2))


@scenario("second_fulfilment", "A second fulfilment picked at Site-B. The live build reads only the first fulfilment and would miss it.")
def _second_fulfilment(gen, sale):
    sale = _at_least_two_lines(gen, sale)
    first, rest = sale.lines[:1], sale.lines[1:]
    return replace(sale, lines=first + tuple(
        replace(line, fulfilment=2, pick_location=SITE_B) for line in rest
    ))


@scenario("internal_customer", "Warehouse-3PL itself is the customer: a stock correction, not a delivery.")
def _internal_customer(gen, sale):
    return replace(sale, customer_id=INTERNAL_CUSTOMER_ID, customer_name="Warehouse-3PL (internal)")


@scenario("novision_note", "#NOVISION in the note: a re-bill for goods already delivered.")
def _novision_note(gen, sale):
    return replace(sale, note=f"#NOVISION re-bill for extras already delivered. {sale.note}")


@scenario("novision_memo", "#NOVISION in the memo rather than the note.")
def _novision_memo(gen, sale):
    return replace(sale, memo="#NOVISION")


@scenario("time_in_instructions", "A delivery time written as free text, not as a window. The warehouse forbids times in instructions.")
def _time_in_instructions(gen, sale):
    return replace(sale, note="Deliver before 10am please, kitchen closed after.")


@scenario("standing_time_in_instructions", "A time in the customer's STANDING comment, copied onto every order. Fix the customer record, don't hold every order.")
def _standing_time_in_instructions(gen, sale):
    note = "Kitchen door only, before 11am."
    return replace(sale, note=note, standing_note=note)


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


@scenario("accented_text", "Accents, ampersands and apostrophes in names, address and notes. The XML must escape and encode them (past LIVE-vs-TEST failure).")
def _accented_text(gen, sale):
    product = (sku("DEM", 4, 2022, "WW"), "Démo Aligoté 2022", 2022, "10.20")
    return replace(
        sale,
        customer_name="Café Zoë & Fils",
        shipping_address=replace(sale.shipping_address, line1="2 O'Neill's Yard"),
        note="Ask for Zoë at the bar & leave in the cellar. 09:00-13:00",
        lines=sale.lines + (gen.line(product, 6),),
    )


@scenario("zero_price_line", "A free sample bottle at zero price alongside paid lines.")
def _zero_price_line(gen, sale):
    sample = (CATALOGUE[4][0], CATALOGUE[4][1], CATALOGUE[4][2], "0.00")
    return replace(sale, lines=sale.lines + (gen.line(sample, 1),))


@scenario("no_delivery_contact", "No contact tagged Delivery. The push falls back to the sale header contact.")
def _no_delivery_contact(gen, sale):
    return replace(sale, contacts=tuple(c for c in sale.contacts if "delivery" not in c.job_title.lower()))


@scenario("multiple_contacts", "Two Delivery contacts; the first has no usable number, so the second must be chosen.")
def _multiple_contacts(gen, sale):
    contacts = (
        Contact("Buyer", "Purchasing", gen.landline(), "", True),
        Contact("Night Porter", "Delivery", "", "", False),
        Contact("Goods In", "Delivery", "", gen.mobile(), False),
    )
    return replace(sale, contacts=contacts, contact_name="Buyer", phone=contacts[0].phone)


@scenario("no_phone", "No usable phone anywhere on the sale or customer. The driver can't call ahead.")
def _no_phone(gen, sale):
    contacts = tuple(replace(c, phone="", mobile="") for c in sale.contacts)
    return replace(sale, phone="", contacts=contacts)


@scenario("missing_town", "Delivery address with no town. The warehouse needs a valid post town.")
def _missing_town(gen, sale):
    return replace(sale, shipping_address=replace(sale.shipping_address, city="", state=""))


@scenario("too_many_lines", "51 lines. The warehouse accepts at most 50 per order.")
def _too_many_lines(gen, sale):
    lines = tuple(
        gen.line((sku("BIN", i, 2022, "RW"), f"Sample Bin {i} 2022", 2022, "9.00"), 6)
        for i in range(1, 52)
    )
    return replace(sale, lines=lines)


@scenario("below_minimum", "12 bottles to a London customer: below the London minimum.")
def _below_minimum(gen, sale):
    customer = gen.customer_in("London")
    lines = (gen.line(CATALOGUE[0], 6), gen.line(CATALOGUE[3], 6))
    return replace(sale, **_customer_fields(customer), lines=lines)


@scenario("below_minimum_scotland", "60 bottles to a Scottish customer: fine for London, below the Scotland minimum.")
def _below_minimum_scotland(gen, sale):
    customer = gen.customer_in("Scotland")
    lines = tuple(gen.line(p, 12) for p in CATALOGUE[:5])
    return replace(sale, **_customer_fields(customer), lines=lines)


@scenario("old_invoice", "Invoiced 10 days ago, pulled back into the window by a payment sync.")
def _old_invoice(gen, sale):
    invoiced = gen.now - timedelta(days=10)
    return replace(
        sale, order_date=invoiced - timedelta(hours=2), invoice_date=invoiced,
        last_modified=gen.now - timedelta(minutes=gen.rng.randint(5, 60)),
    )


@scenario("across_clock_change", "Ordered in British Summer Time, invoiced after the clocks went back.")
def _across_clock_change(gen, sale):
    return replace(sale, order_date=datetime(2026, 10, 24, 16, 30, tzinfo=UK))


@scenario("new_customer", "First order from a customer the system has never seen.")
def _new_customer(gen, sale):
    customer = gen._customer(len(gen.customers) + 1)
    return replace(sale, **_customer_fields(customer), lines=gen.lines_for(customer))


# A day that touches the main rules, topped up with standard sales.
DEFAULT_DAY_MIX = {
    "not_invoiced": 2,
    "shopify": 1,
    "shopify_warehouse_pick": 1,
    "site_b_pick": 1,
    "mixed_pick": 1,
    "internal_customer": 1,
    "novision_note": 1,
    "time_in_instructions": 1,
    "below_minimum": 1,
    "new_customer": 1,
}
