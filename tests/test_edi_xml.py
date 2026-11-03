"""The warehouse XML matches the live rules: singles, price, duty paid,
postcode as delivery point, +44 phone, window out of the instructions."""

import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import date

import pytest

from chaain.connectors.stock_system.generator import SaleGenerator
from chaain.connectors.warehouse.edi_xml import (
    DECLARATION, EdiSettings, build, parse_window, resolve_contact, split_address, uk_phone,
)
from chaain.core.config import load_settings
from chaain.core.models import Contact

SETTINGS = EdiSettings()
TODAY = date(2026, 11, 4)


@pytest.fixture
def gen():
    return SaleGenerator(seed=3)


def xml_of(sale):
    order = build(sale, SETTINGS, TODAY)
    return order, ET.fromstring(order.payload)


def text(root, path):
    found = root.find(path)
    return None if found is None else found.text


# ---------- the document ----------

def test_declaration_is_exactly_what_the_live_warehouse_accepts(gen):
    order, _ = xml_of(gen.make())
    assert order.payload.startswith(DECLARATION.encode())
    assert DECLARATION == '<?xml version="1.0" encoding="UTF-8"?>'
    assert b"<DeliveryAddress2></DeliveryAddress2>" in order.payload  # as live, not <Tag />


def test_accents_ampersands_and_apostrophes_survive_as_valid_utf8(gen):
    sale = gen.make("accented_text")
    order, root = xml_of(sale)
    assert text(root, "SO/HD/DeliveryName") == "Café Zoë & Fils"
    assert text(root, "SO/HD/DeliveryAddress1") == "2 O'Neill's Yard"
    assert b"&amp;" in order.payload and "Zoë".encode("utf-8") in order.payload


def test_core_fields(gen):
    sale = gen.make()
    _, root = xml_of(sale)
    assert text(root, "SO/HD/DocumentRef") == sale.so_number
    assert text(root, "SO/HD/FKUnderBondType") == "P"
    assert text(root, "SO/HD/DPAccountRef") == sale.shipping_address.postcode
    assert text(root, "SO/HD/OrderDate") == "04/11/2026"
    lines = root.findall("SO/LD")
    assert [l.findtext("SinglesOrdered") for l in lines] == [str(l.qty) for l in sale.lines]
    assert [l.findtext("SinglePrice") for l in lines] == [f"{l.price:.2f}" for l in sale.lines]


def test_zero_price_sample_is_sent_as_zero(gen):
    _, root = xml_of(gen.make("zero_price_line"))
    assert root.findall("SO/LD")[-1].findtext("SinglePrice") == "0.00"


def test_settings_come_from_the_client_profile():
    settings = EdiSettings.from_config(load_settings("demo")["warehouse"])
    assert settings.account_code == "DEMO" and settings.min_window_minutes == 120


# ---------- delivery window and instructions ----------

@pytest.mark.parametrize("note, earliest, latest, instructions, source", [
    ("Deliver to side entrance. 09:00-13:00", "09:00", "13:00", "Deliver to side entrance", "parsed"),
    ("Back door 10:00-11:00", "10:00", "12:00", "Back door", "widened"),
    ("Leave with bar staff.", "08:00", "17:00", "Leave with bar staff.", "default"),
    # Loose times aren't parsed: they stay visible for the gate to catch.
    ("Deliver before 10am please", "08:00", "17:00", "Deliver before 10am please", "default"),
    ("Side entrance. 9-1", "08:00", "17:00", "Side entrance. 9-1", "default"),
    ("Closed 14:00-12:00", "08:00", "17:00", "Closed 14:00-12:00", "default"),
])
def test_window_parsing(note, earliest, latest, instructions, source):
    w = parse_window(note, SETTINGS)
    assert (w.earliest, w.latest, w.instructions, w.source) == (earliest, latest, instructions, source)


def test_placeholder_means_no_instructions_field(gen):
    sale = gen.make(note="[No Delivery Instructions]")
    _, root = xml_of(sale)
    assert root.find("SO/HO/DeliveryInstructions1") is None
    assert root.find("SO/HO/CustomerPORef") is None  # blank optional fields are left out


def test_long_instructions_are_truncated_and_flagged(gen):
    order, root = xml_of(gen.make("long_instructions"))
    sent = text(root, "SO/HO/DeliveryInstructions1")
    assert len(sent) <= 120 and sent.endswith("...")
    assert "09:00" not in sent
    assert order.flags["instructions_truncated"] is True


# ---------- addresses and phones ----------

def test_long_first_line_splits_at_a_word():
    a, b, split = split_address("Unit 4 The Old Brewery Buildings Off Demo Lane Industrial Estate", "Rear")
    assert split and len(a) <= 50 and not a.endswith(" ")
    assert b.endswith(", Rear")


@pytest.mark.parametrize("raw, expected", [
    ("07700 900123", "+447700900123"),
    ("+44 7700 900123", "+447700900123"),
    ("0044 7700 900123", "+447700900123"),
    ("7700900123", "+447700900123"),
    ("020 7946 0123", "+442079460123"),
    ("12345", None),
    ("", None),
])
def test_uk_phone(raw, expected):
    assert uk_phone(raw) == expected


def test_delivery_contact_wins_over_the_default(gen):
    sale = gen.make(contacts=(
        Contact("Buyer", "Purchasing", "020 7946 0001", "", True),
        Contact("Goods In", "Delivery", "", "07700 900222", False),
    ))
    assert resolve_contact(sale) == ("Goods In", "+447700900222", "delivery tag: Goods In")


def test_skips_a_delivery_contact_with_no_number(gen):
    name, _, _ = resolve_contact(gen.make("multiple_contacts"))
    assert name == "Goods In"


def test_falls_back_to_the_sale_header(gen):
    sale = gen.make("no_delivery_contact")
    assert resolve_contact(sale)[2] == "sale header"


# ---------- what the warehouse would refuse ----------

@pytest.mark.parametrize("scenario, problem", [
    ("no_phone", "phone"),
    ("missing_town", "town"),
    ("too_many_lines", "51 lines"),
])
def test_problems_are_reported_not_sent(gen, scenario, problem):
    order, _ = xml_of(gen.make(scenario))
    assert len(order.problems) == 1 and problem in order.problems[0]


def test_town_falls_back_to_state(gen):
    sale = gen.make()
    sale = replace(sale, shipping_address=replace(sale.shipping_address, city="", state="Cornwall"))
    order, root = xml_of(sale)
    assert text(root, "SO/HD/DeliveryTownOrCounty") == "Cornwall" and not order.problems


def test_standard_sale_has_no_problems(gen):
    assert all(not build(s, SETTINGS, TODAY).problems for s in (gen.make() for _ in range(20)))
