"""The order gate: AUTO, HOLD (with every reason) or SKIP (with a reason).

A pure function: the same sale and reference data always give the same
answer, and nothing is read or written along the way. Values come from
configs/<client>/rules.yaml; the logic is here, with tests.

SKIPs are checked first, because an order that isn't for the warehouse
doesn't need anyone's judgement. Then every HOLD rule runs, so the team sees
all the reasons at once, in the order they should act on them:
credit (the Finance lead), stock and vintage (the customer may need telling),
locations, data fixes in the stock system, then the customer and the notes.

The demo assumes AUTO orders are confirmed and picked on arrival, with no
draft step to catch a mistake (an open question). A HOLD is then the only
safety net, which is why each rule's
mode (hold / report / off) is set deliberately in config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from chaain.core.decision import Decision, Route
from chaain.core.models import Sale, SaleStatus, SourceChannel

HOLD, REPORT, OFF = "hold", "report", "off"
RULES = (
    "credit_hold", "stock_shortfall", "vintage_change", "reserved_stock",
    "mixed_locations", "shopify_at_warehouse", "data_defects",
    "new_customer", "below_minimum", "notes",
)


# ---------- inputs ----------

@dataclass(frozen=True)
class NoteFlag:
    name: str
    raised: bool
    confidence: float
    explanation: str


@dataclass(frozen=True)
class NotesResult:
    """What the notes reader found. `error` means it couldn't read this order's
    changes; `standing` are problems in the customer's standing comment."""

    flags: tuple[NoteFlag, ...] = ()
    error: str | None = None
    standing: tuple[NoteFlag, ...] = ()
    standing_error: str | None = None
    source: str = ""  # prompt version and model, for the audit trail


@dataclass(frozen=True)
class Reference:
    """The facts the gate needs beyond the sale, gathered once per run."""

    credit_hold_customers: frozenset[str] = frozenset()  # the stock system's credit-hold flag
    warehouse_stock: dict[str, int] = field(default_factory=dict)  # bottles physically held, by SKU
    product_flags: dict[str, str] = field(default_factory=dict)  # SKU -> RESERVE FOR detail
    known_customers: frozenset[str] = frozenset()  # customers with a previous order sent
    last_vintages: dict[str, dict[str, int | None]] = field(default_factory=dict)  # customer -> wine -> vintage


@dataclass(frozen=True)
class GateRules:
    warehouse: str
    second_site: str
    internal_customer_ids: frozenset[str]
    marker: re.Pattern
    delivery_charge_sku: str
    default_minimum: int
    bands: tuple[tuple[str, int, frozenset[str]], ...]  # (name, bottles, postcode areas)
    safety_margin: int
    confidence_threshold: float
    modes: dict[str, str]

    @classmethod
    def from_config(cls, rules: dict) -> "GateRules":
        modes = {name: rules["rules"].get(name, HOLD) for name in RULES}
        bad = {n: m for n, m in modes.items() if m not in (HOLD, REPORT, OFF)}
        if bad:
            raise ValueError(f"rule modes must be hold, report or off: {bad}")
        marker = rules["do_not_send_marker"].lstrip("#")
        return cls(
            warehouse=rules["locations"]["warehouse"],
            second_site=rules["locations"]["second_site"],
            internal_customer_ids=frozenset(rules["internal_customer_ids"]),
            marker=re.compile(rf"#\s*{re.escape(marker)}\b", re.IGNORECASE),
            delivery_charge_sku=rules["minimums"]["delivery_charge_sku"],
            default_minimum=rules["minimums"]["default_bottles"],
            bands=tuple(
                (b["name"], b["bottles"], frozenset(b["areas"])) for b in rules["minimums"]["bands"]
            ),
            safety_margin=rules["stock"]["safety_margin_bottles"],
            confidence_threshold=rules["notes"]["confidence_threshold"],
            modes=modes,
        )


# ---------- the gate ----------

def decide(
    sale: Sale, ref: Reference, rules: GateRules,
    problems: tuple[str, ...] = (), notes: NotesResult | None = None,
) -> Decision:
    """`problems` are what the warehouse format can't accept (from
    `Warehouse.prepare`); `notes` is the notes reader's result, or None if it
    wasn't needed."""
    skip = _skip(sale, rules)
    if skip:
        return skip

    holds, reports = [], []
    for name, check in _CHECKS:
        findings = check(sale, ref, rules, problems, notes)
        mode = rules.modes[name]
        if findings and mode == HOLD:
            holds += findings
        elif findings and mode == REPORT:
            reports += findings
    if notes is not None and rules.modes["notes"] != OFF:
        reports += _standing(notes)
    if holds:
        return Decision(Route.HOLD, tuple(holds), reports=tuple(reports))
    return Decision(Route.AUTO, (), reports=tuple(reports))


def _skip(sale: Sale, rules: GateRules) -> Decision | None:
    if sale.status in (SaleStatus.CREDITED, SaleStatus.VOIDED):
        return Decision(Route.SKIP, (f"{sale.status.value} after invoicing",), final=True)
    if rules.marker.search(sale.note) or rules.marker.search(sale.memo):
        return Decision(Route.SKIP, ("Marked #NOVISION: invoice only, never sent",), final=True)
    if sale.status is not SaleStatus.INVOICED:
        return Decision(Route.SKIP, (f"Not invoiced yet ({sale.status.value})",))
    if sale.customer_id in rules.internal_customer_ids:
        return Decision(Route.SKIP, ("Internal customer: a stock correction, not a delivery",))
    if _goods(sale, rules) and all(l.pick_location == rules.second_site for l in _goods(sale, rules)):
        channel = "Shopify order" if sale.source_channel is SourceChannel.SHOPIFY else "Order"
        return Decision(Route.SKIP, (f"{channel} picked at {rules.second_site}: not for the warehouse",))
    return None


def _goods(sale: Sale, rules: GateRules):
    """Lines that are bottles, not the delivery charge."""
    return [l for l in sale.lines if l.sku != rules.delivery_charge_sku]


# ---------- the HOLD rules, in the order a person should act on them ----------

def _credit_hold(sale, ref, rules, problems, notes):
    if sale.customer_id in ref.credit_hold_customers:
        return ["Customer is on credit hold: the Finance lead decides"]
    return []


def _stock_shortfall(sale, ref, rules, problems, notes):
    needed: dict[str, int] = {}
    for l in _goods(sale, rules):
        if l.pick_location == rules.warehouse:
            needed[l.sku] = needed.get(l.sku, 0) + l.qty
    return [
        f"{sku}: order needs {qty}, the warehouse physically holds {ref.warehouse_stock.get(sku, 0)}"
        for sku, qty in sorted(needed.items())
        if qty > ref.warehouse_stock.get(sku, 0) - rules.safety_margin
    ]


def _vintage_change(sale, ref, rules, problems, notes):
    last = ref.last_vintages.get(sale.customer_id, {})
    out = []
    for l in _goods(sale, rules):
        wine = l.sku[:5]  # supplier + wine number, per the SKU convention
        if wine in last and last[wine] != l.vintage:
            was, now = last[wine] or "NV", l.vintage or "NV"
            out.append(f"{l.product_name}: vintage {now}, customer last had {was}")
    return out


def _reserved_stock(sale, ref, rules, problems, notes):
    return [
        f"{l.sku} is flagged RESERVE FOR: {ref.product_flags[l.sku]}"
        for l in _goods(sale, rules) if l.sku in ref.product_flags
    ]


def _mixed_locations(sale, ref, rules, problems, notes):
    goods = _goods(sale, rules)
    at_site_b = [l for l in goods if l.pick_location == rules.second_site]
    if at_site_b and len(at_site_b) < len(goods):
        return [f"Mixed pick locations: {len(at_site_b)} of {len(goods)} lines picked at {rules.second_site}"]
    if sale.header_location == rules.second_site:
        return [f"Header says {rules.second_site} but stock was picked at {rules.warehouse}"]
    return []


def _shopify_at_warehouse(sale, ref, rules, problems, notes):
    if sale.source_channel is SourceChannel.SHOPIFY:
        return [f"Shopify order picked at {rules.warehouse}: Shopify isn't pushed, so send it by hand or confirm"]
    return []


def _data_defects(sale, ref, rules, problems, notes):
    return list(problems)


def _new_customer(sale, ref, rules, problems, notes):
    if sale.customer_id not in ref.known_customers:
        return ["First order for this customer: check their warehouse account, delivery contact and postcode band"]
    return []


def _below_minimum(sale, ref, rules, problems, notes):
    if any(l.sku == rules.delivery_charge_sku for l in sale.lines):
        return []  # below the band is fine when delivery is charged
    name, minimum = _band(sale.shipping_address.postcode, rules)
    bottles = sum(l.qty for l in _goods(sale, rules))
    if bottles < minimum:
        return [f"{bottles} bottles is below the {name} minimum of {minimum}, and no delivery charge"]
    return []


def _notes(sale, ref, rules, problems, notes):
    if notes is None:
        return []
    tag = f" [{notes.source}]" if notes.source else ""
    if notes.error:
        return [f"Notes reader unavailable ({notes.error}): read the note before sending{tag}"]
    out = []
    for f in notes.flags:
        if f.raised:
            out.append(f"Note: {f.name.replace('_', ' ')}: {f.explanation}{tag}")
        elif f.confidence < rules.confidence_threshold:
            out.append(f"Note: unsure about {f.name.replace('_', ' ')} ({f.confidence:.0%}): {f.explanation}{tag}")
    return out


def _standing(notes: NotesResult) -> list[str]:
    """Problems in the standing comment: fix the record once, don't hold every order."""
    out = [
        f"Customer's standing comment: {f.name.replace('_', ' ')}: {f.explanation} Fix the customer record."
        for f in notes.standing
    ]
    if notes.standing_error:
        out.append(f"Couldn't check the customer's standing comment ({notes.standing_error})")
    return out


def _band(postcode: str, rules: GateRules) -> tuple[str, int]:
    area = re.match(r"[A-Z]+", postcode.strip().upper())
    letters = area.group(0) if area else ""
    for name, bottles, areas in rules.bands:
        if letters in areas:
            return name, bottles
    return "default", rules.default_minimum


_CHECKS: tuple[tuple[str, Callable], ...] = (
    ("credit_hold", _credit_hold),
    ("stock_shortfall", _stock_shortfall),
    ("vintage_change", _vintage_change),
    ("reserved_stock", _reserved_stock),
    ("mixed_locations", _mixed_locations),
    ("shopify_at_warehouse", _shopify_at_warehouse),
    ("data_defects", _data_defects),
    ("new_customer", _new_customer),
    ("below_minimum", _below_minimum),
    ("notes", _notes),
)
assert tuple(name for name, _ in _CHECKS) == RULES


def gate_for(
    ref: Reference, rules: GateRules, prepare: Callable,
    read_notes: Callable[[Sale], NotesResult | None] = lambda sale: None,
) -> Callable[[Sale], Decision]:
    """The gate as the run calls it: one sale in, one decision out.
    `prepare` is the warehouse's, so its format limits become HOLD reasons."""
    def gate(sale: Sale) -> Decision:
        return decide(sale, ref, rules, prepare(sale).problems, read_notes(sale))
    return gate
