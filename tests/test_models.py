"""The sale model rejects bad data at the door and can't be changed later."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from chaain.core.models import Address, Contact, Sale, SaleLine, SaleStatus, SourceChannel

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def line(**overrides):
    fields = dict(
        sku="CW-R001", product_name="Demo Red", vintage=2021, qty=6,
        price=Decimal("9.50"), pick_location="Warehouse-3PL",
    )
    return SaleLine(**{**fields, **overrides})


def sale(**overrides):
    fields = dict(
        id="0f8fad5b-d9cb-469f-a165-70867728950e", so_number="SO-10001",
        status=SaleStatus.INVOICED, source_channel=SourceChannel.WHOLESALE,
        customer_id="c1", customer_name="Demo Venue 01", contact_name="Duty Manager",
        phone="07700 900001",
        contacts=(Contact("Duty Manager", "Manager", "07700 900001", "", True),),
        shipping_address=Address("1 Demo Street", "", "London", "", "EC1A 9ZZ"),
        customer_reference="", header_location="Warehouse-3PL", note="", memo="",
        order_date=NOW, invoice_date=NOW, last_modified=NOW,
        lines=(line(), line(sku="CW-W002", qty=12)),
    )
    return Sale(**{**fields, **overrides})


def test_valid_sale_builds_and_derives_totals():
    s = sale()
    assert s.total_bottles == 18
    assert s.pick_locations == {"Warehouse-3PL"}


def test_mixed_pick_locations_are_visible():
    s = sale(lines=(line(), line(pick_location="Site-B")))
    assert s.pick_locations == {"Warehouse-3PL", "Site-B"}


def test_lines_from_every_fulfilment_are_kept():
    s = sale(lines=(line(), line(fulfilment=2, pick_location="Site-B")))
    assert s.fulfilments == {1, 2}
    assert "Site-B" in s.pick_locations


def test_fulfilment_numbers_start_at_one():
    with pytest.raises(ValueError):
        line(fulfilment=0)


def test_zero_price_sample_line_is_allowed():
    assert line(price=Decimal("0"), qty=1).price == 0


def test_sale_cannot_be_changed_after_building():
    s = sale()
    with pytest.raises(FrozenInstanceError):
        s.status = SaleStatus.DRAFT
    # Changes make a new copy; the original is untouched.
    assert replace(s, status=SaleStatus.DRAFT).status is SaleStatus.DRAFT
    assert s.status is SaleStatus.INVOICED


@pytest.mark.parametrize("qty", [0, -6, 1.5, True])
def test_quantity_must_be_positive_whole_bottles(qty):
    with pytest.raises(ValueError):
        line(qty=qty)


def test_unknown_status_rejected():
    with pytest.raises(ValueError):
        sale(status="COMPLETED")


def test_naive_timestamp_rejected():
    with pytest.raises(ValueError):
        sale(last_modified=datetime(2026, 11, 4, 12, 0))


def test_lines_and_contacts_must_be_tuples():
    with pytest.raises(ValueError):
        sale(lines=[line()])
    with pytest.raises(ValueError):
        sale(contacts=[])
