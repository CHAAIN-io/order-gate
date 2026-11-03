"""The generator builds each awkward case on demand; the mock serves them
the way the real stock system would."""

from datetime import timedelta

import pytest

from chaain.connectors.stock_system.generator import (
    CATALOGUE, DEFAULT_DAY_MIX, INTERNAL_CUSTOMER_ID, SCENARIOS, SITE_B, TYPICAL_MINIMUM,
    WAREHOUSE, SaleGenerator,
)
from chaain.connectors.stock_system.interface import StockSystem
from chaain.connectors.stock_system.mock import MockStockSystem
from chaain.core.models import SaleStatus, SourceChannel


@pytest.fixture
def gen():
    return SaleGenerator(seed=1)


# ---------- generator ----------

def test_same_seed_gives_same_sales():
    assert SaleGenerator(seed=7).day() == SaleGenerator(seed=7).day()


def test_different_seeds_differ():
    assert SaleGenerator(seed=7).day() != SaleGenerator(seed=8).day()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_builds_a_valid_sale(gen, name):
    sale = gen.make(name)
    assert sale.so_number.startswith("SO-")
    assert sale.lines


def test_standard_sale_is_clean(gen):
    sale = gen.make("standard")
    assert sale.status is SaleStatus.INVOICED
    assert sale.source_channel is SourceChannel.WHOLESALE
    assert sale.header_location == WAREHOUSE
    assert sale.pick_locations == {WAREHOUSE}
    assert "#NOVISION" not in sale.note + sale.memo
    assert sale.total_bottles >= 24


def test_site_b_pick_hides_behind_warehouse_header(gen):
    sale = gen.make("site_b_pick")
    assert sale.header_location == WAREHOUSE
    assert sale.pick_locations == {SITE_B}


def test_mixed_pick_has_both_locations(gen):
    assert gen.make("mixed_pick").pick_locations == {WAREHOUSE, SITE_B}


def test_not_invoiced_has_no_invoice_date(gen):
    sale = gen.make("not_invoiced")
    assert sale.status is not SaleStatus.INVOICED
    assert sale.invoice_date is None


def test_novision_can_sit_in_note_or_memo(gen):
    assert "#NOVISION" in gen.make("novision_note").note
    assert "#NOVISION" in gen.make("novision_memo").memo


def test_vintage_matches_the_sku_convention():
    for code, _, vintage, _ in CATALOGUE:
        assert code[5:7] == ("NV" if vintage is None else f"{vintage % 100:02d}")


def test_ordinary_sales_clear_their_region_minimum(gen):
    region = {c.id: c.region for c in gen.customers}
    for sale in (gen.make("standard") for _ in range(30)):
        assert sale.total_bottles >= TYPICAL_MINIMUM[region[sale.customer_id]]


def test_below_minimum_depends_on_region(gen):
    region = {c.id: c.region for c in gen.customers}
    london = gen.make("below_minimum")
    scotland = gen.make("below_minimum_scotland")
    assert region[london.customer_id] == "London" and london.total_bottles < 24
    assert region[scotland.customer_id] == "Scotland" and 24 <= scotland.total_bottles < 120


def test_shopify_default_is_site_b_but_can_be_switched_to_warehouse(gen):
    default = gen.make("shopify")
    switched = gen.make("shopify_warehouse_pick")
    assert default.source_channel is switched.source_channel is SourceChannel.SHOPIFY
    assert default.pick_locations == {SITE_B}
    assert switched.pick_locations == {WAREHOUSE}


def test_second_fulfilment_is_visible_and_picked_at_site_b(gen):
    sale = gen.make("second_fulfilment")
    assert sale.fulfilments == {1, 2}
    assert {l.pick_location for l in sale.lines if l.fulfilment == 2} == {SITE_B}


def test_credited_after_invoicing(gen):
    assert gen.make("credited").status is SaleStatus.CREDITED


def test_header_contact_is_copied_from_the_default_contact(gen):
    sale = gen.make("standard")
    default = next(c for c in sale.contacts if c.is_default)
    assert (sale.contact_name, sale.phone) == (default.name, default.phone)


def test_contact_scenarios(gen):
    assert not any("Delivery" in c.job_title for c in gen.make("no_delivery_contact").contacts)
    tagged = [c for c in gen.make("multiple_contacts").contacts if c.job_title == "Delivery"]
    assert len(tagged) == 2 and not tagged[0].mobile and tagged[1].mobile
    nophone = gen.make("no_phone")
    assert nophone.phone == "" and all(not (c.phone or c.mobile) for c in nophone.contacts)


def test_accented_text_and_zero_price(gen):
    accented = gen.make("accented_text")
    assert "é" in accented.customer_name and "&" in accented.note and "'" in accented.shipping_address.line1
    assert any(l.price == 0 for l in gen.make("zero_price_line").lines)


def test_clock_change_offsets(gen):
    sale = gen.make("across_clock_change")
    assert sale.order_date.utcoffset() == timedelta(hours=1)  # BST
    assert sale.invoice_date.utcoffset() == timedelta(0)  # GMT


def test_internal_customer(gen):
    assert gen.make("internal_customer").customer_id == INTERNAL_CUSTOMER_ID


def test_old_invoice_is_recently_modified(gen):
    sale = gen.make("old_invoice")
    assert gen.now - sale.invoice_date >= timedelta(days=7)
    assert gen.now - sale.last_modified < timedelta(hours=2)


def test_new_customer_is_not_in_the_pool(gen):
    sale = gen.make("new_customer")
    assert sale.customer_id not in {c.id for c in gen.customers}


def test_too_many_lines(gen):
    assert len(gen.make("too_many_lines").lines) == 51


def test_overrides_apply_on_top_of_scenario(gen):
    sale = gen.make("shopify", note="Ring on arrival.")
    assert sale.source_channel is SourceChannel.SHOPIFY
    assert sale.note == "Ring on arrival."


def test_unknown_scenario_names_the_known_ones(gen):
    with pytest.raises(KeyError, match="site_b_pick"):
        gen.make("no_such_case")


def test_so_numbers_and_ids_are_unique_across_a_day(gen):
    sales = gen.day(n=40)
    # Consecutive, with no gaps a reader would mistake for missing orders.
    assert [s.so_number for s in sorted(sales, key=lambda s: s.so_number)] == [
        f"SO-{n:05d}" for n in range(10001, 10041)
    ]
    assert len({s.id for s in sales}) == 40


def test_day_includes_the_mix_and_tops_up(gen):
    sales = gen.day(n=20)
    assert len(sales) == 20
    shopify = [s for s in sales if s.source_channel is SourceChannel.SHOPIFY]
    assert len(shopify) == DEFAULT_DAY_MIX["shopify"] + DEFAULT_DAY_MIX["shopify_warehouse_pick"]


def test_day_rejects_a_mix_larger_than_n(gen):
    with pytest.raises(ValueError):
        gen.day(n=3, mix={"shopify": 4})


# ---------- mock stock system ----------

def test_mock_implements_the_interface():
    assert isinstance(MockStockSystem(), StockSystem)


def test_lists_only_sales_changed_since_cutoff_oldest_first(gen):
    sales = gen.day(n=20)
    stock = MockStockSystem(sales)
    cutoff = gen.now - timedelta(minutes=90)

    found = stock.list_sales_updated_since(cutoff)

    assert 0 < len(found) < len(sales)  # the cutoff really splits the day
    assert found ==sorted((s for s in sales if s.last_modified >= cutoff), key=lambda s: s.last_modified)


def test_payment_sync_brings_an_old_sale_back_into_the_window(gen):
    sale = gen.make("standard", last_modified=gen.now - timedelta(days=5))
    stock = MockStockSystem([sale])
    cutoff = gen.now - timedelta(hours=48)
    assert stock.list_sales_updated_since(cutoff) == []

    stock.update(sale.id, modified_at=gen.now)

    [back] = stock.list_sales_updated_since(cutoff)
    assert back.so_number == sale.so_number
    assert back.lines == sale.lines


def test_get_sale(gen):
    sale = gen.make()
    stock = MockStockSystem([sale])
    assert stock.get_sale(sale.id) == sale
    assert stock.get_sale("missing") is None
