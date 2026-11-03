"""The gate routes every scenario correctly, lists every reason in a useful
order, and each rule's mode (hold / report / off) comes from config."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from chaain.connectors.stock_system.generator import CATALOGUE, DEFAULT_NOW, SCENARIOS, SaleGenerator
from chaain.connectors.stock_system.mock import MockStockSystem
from chaain.connectors.warehouse.mock import MockWarehouse
from chaain.core.config import load_rules
from chaain.core.decision import Route
from chaain.core.ledger.sqlite import SqliteLedger
from chaain.core.models import SaleLine, SaleStatus
from chaain.workflows.order_push.gate import (
    GateRules, NoteFlag, NotesResult, Reference, decide, gate_for,
)
from chaain.workflows.order_push.run import RunSettings, run_once

RULES = GateRules.from_config(load_rules("demo"))


@pytest.fixture
def gen():
    return SaleGenerator(seed=21)


@pytest.fixture
def ref(gen):
    stock = {code: 1000 for code, *_ in CATALOGUE}
    stock.update({"DEM0422WW": 1000, **{f"BIN{i:02d}22RW": 1000 for i in range(1, 52)}})
    return Reference(warehouse_stock=stock, known_customers=frozenset(c.id for c in gen.customers))


def route(sale, ref, rules=RULES, notes=None):
    problems = MockWarehouse(today=date(2026, 11, 4)).prepare(sale).problems
    return decide(sale, ref, rules, problems, notes)


def with_modes(**modes):
    return replace(RULES, modes={**RULES.modes, **modes})


# ---------- every scenario ----------

EXPECTED = {
    "standard": Route.AUTO,
    "not_invoiced": Route.SKIP,
    "credited": Route.SKIP,
    "shopify": Route.SKIP,
    "shopify_warehouse_pick": Route.HOLD,
    "site_b_header": Route.SKIP,
    "site_b_pick": Route.SKIP,
    "mixed_pick": Route.HOLD,
    "second_fulfilment": Route.HOLD,
    "internal_customer": Route.SKIP,
    "novision_note": Route.SKIP,
    "novision_memo": Route.SKIP,
    # Free-text times and other note problems are the notes reader's job
    # (Stage 2.2). Until then these go AUTO: the gap the AI step closes.
    "time_in_instructions": Route.AUTO,
    "no_window": Route.AUTO,
    "long_instructions": Route.AUTO,
    "accented_text": Route.AUTO,
    "zero_price_line": Route.AUTO,
    "no_delivery_contact": Route.AUTO,
    "multiple_contacts": Route.AUTO,
    "no_phone": Route.HOLD,
    "missing_town": Route.HOLD,
    "too_many_lines": Route.HOLD,
    "below_minimum": Route.AUTO,  # report only by default
    "below_minimum_scotland": Route.AUTO,
    "old_invoice": Route.AUTO,  # the run ignores it before the gate sees it
    "across_clock_change": Route.AUTO,
    "new_customer": Route.HOLD,
}


def test_every_scenario_has_an_expected_route():
    assert set(EXPECTED) == set(SCENARIOS)


@pytest.mark.parametrize("scenario", sorted(EXPECTED))
def test_scenario_routes(gen, ref, scenario):
    decision = route(gen.make(scenario), ref)
    assert decision.route is EXPECTED[scenario], decision


# ---------- the brief's numbered tests ----------

def test_4_picked_at_site_b_is_skipped_even_with_a_warehouse_header(gen, ref):
    decision = route(gen.make("site_b_pick"), ref)
    assert decision.route is Route.SKIP and "Site-B" in decision.reasons[0]
    assert not decision.final  # re-checked if someone corrects the pick


def test_5_shopify_is_skipped_at_site_b_but_held_at_the_warehouse(gen, ref):
    assert route(gen.make("shopify"), ref).route is Route.SKIP
    held = route(gen.make("shopify_warehouse_pick"), ref)
    assert held.route is Route.HOLD and "send it by hand" in held.reasons[0]


@pytest.mark.parametrize("field, text", [
    ("note", "#NOVISION re-bill"), ("memo", "#NOVISION"), ("note", "re-bill # novision"),
])
def test_6_novision_is_a_final_skip(gen, ref, field, text):
    decision = route(gen.make(**{field: text}), ref)
    assert decision.route is Route.SKIP and decision.final


def test_9_credit_hold(gen, ref):
    sale = gen.make()
    decision = route(sale, replace(ref, credit_hold_customers=frozenset({sale.customer_id})))
    assert decision.route is Route.HOLD and "Finance lead" in decision.reasons[0]


def test_10_mixed_locations(gen, ref):
    decision = route(gen.make("mixed_pick"), ref)
    assert decision.route is Route.HOLD and "Mixed pick locations" in decision.reasons[0]


# ---------- reasons and modes ----------

def test_every_reason_is_listed_in_the_order_to_act_on(gen, ref):
    sale = gen.make("mixed_pick")
    ref = replace(ref, credit_hold_customers=frozenset({sale.customer_id}), known_customers=frozenset())
    reasons = route(sale, ref).reasons
    assert [r.split(":")[0].split(" ")[0] for r in reasons] == ["Customer", "Mixed", "First"]


def test_report_mode_shows_but_does_not_hold(gen, ref):
    sale = gen.make("below_minimum")
    decision = route(sale, ref)
    assert decision.route is Route.AUTO and "below the London minimum of 24" in decision.reports[0]

    held = route(sale, ref, with_modes(below_minimum="hold"))
    assert held.route is Route.HOLD
    off = route(sale, ref, with_modes(below_minimum="off"))
    assert off.route is Route.AUTO and off.reports == ()


def test_invalid_mode_is_refused():
    config = load_rules("demo")
    config["rules"]["credit_hold"] = "maybe"
    with pytest.raises(ValueError, match="hold, report or off"):
        GateRules.from_config(config)


# ---------- individual rules ----------

def test_minimum_depends_on_region_and_a_delivery_charge_clears_it(gen, ref):
    scotland = route(gen.make("below_minimum_scotland"), ref)
    assert "Scotland minimum of 120" in scotland.reports[0]

    sale = gen.make("below_minimum")
    charge = SaleLine("DELIVERY", "Delivery charge", None, 1, Decimal("9.00"), "Warehouse-3PL")
    assert route(replace(sale, lines=sale.lines + (charge,)), ref).reports == ()


def test_stock_shortfall_counts_only_warehouse_lines(gen, ref):
    sale = gen.make("mixed_pick")
    warehouse_sku = next(l.sku for l in sale.lines if l.pick_location == "Warehouse-3PL")
    site_b_sku = next(l.sku for l in sale.lines if l.pick_location == "Site-B")
    short = replace(ref, warehouse_stock={**ref.warehouse_stock, warehouse_sku: 0, site_b_sku: 0})
    reasons = route(sale, short).reasons
    assert any(r.startswith(warehouse_sku) for r in reasons)
    assert not any(r.startswith(site_b_sku) for r in reasons)


def test_vintage_change_and_reserved_stock_are_reported(gen, ref):
    sale = gen.make()
    line = sale.lines[0]
    ref = replace(
        ref,
        last_vintages={sale.customer_id: {line.sku[:5]: 2015}},
        product_flags={line.sku: "Demo Venue 99, 24 bottles"},
    )
    decision = route(sale, ref)
    assert decision.route is Route.AUTO
    assert any("customer last had 2015" in r for r in decision.reports)
    assert any("RESERVE FOR: Demo Venue 99" in r for r in decision.reports)


def test_header_at_site_b_with_warehouse_picks_is_held(gen, ref):
    decision = route(gen.make(header_location="Site-B"), ref)
    assert decision.route is Route.HOLD and "Header says Site-B" in decision.reasons[0]


@pytest.mark.parametrize("notes, held", [
    (NotesResult((NoteFlag("time_in_instructions", True, 0.95, "Says 'before 10am'"),)), True),
    (NotesResult((NoteFlag("invoice_only", False, 0.55, "Mentions a re-bill"),)), True),
    (NotesResult((NoteFlag("invoice_only", False, 0.97, "Ordinary instructions"),)), False),
    (NotesResult(error="API timeout"), True),
    (None, False),
])
def test_notes_can_only_add_holds(gen, ref, notes, held):
    assert (route(gen.make(), ref, notes=notes).route is Route.HOLD) is held


# ---------- in the run ----------

def test_a_gated_day_sends_only_auto_orders(gen, ref):
    sales = gen.day(n=20)
    expected_auto = {s.so_number for s in sales if route(s, ref).route is Route.AUTO}
    wh = MockWarehouse(today=date(2026, 11, 4))

    report = run_once(
        DEFAULT_NOW, MockStockSystem(sales), SqliteLedger(), wh,
        gate_for(ref, RULES, wh.prepare), RunSettings(per_run_cap=20),
    )

    assert set(wh.attempts) == expected_auto
    # Every invoiced sale got exactly one decision; not-invoiced ones wait, with no row.
    decided = [so for so, action, _ in report.actions if action in ("sent", "held", "skipped")]
    invoiced = [s.so_number for s in sales if s.status is SaleStatus.INVOICED]
    assert sorted(decided) == sorted(invoiced)
