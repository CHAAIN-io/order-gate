"""Vision-style EDI order XML, ported from the live workflow.

Everything here is about the warehouse's format: field lengths, codes, the
2-hour window, the 50-line limit. A different warehouse would get its own
module; core code never sees any of this.

The XML is built as a tree rather than pasted together as text, so special
characters (&, <, accents) are always escaped correctly. The declaration is
written by hand because the live warehouse rejected the single-quoted,
lower-case form that some libraries produce, after the test system had
accepted it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date

from chaain.connectors.warehouse.interface import PreparedOrder
from chaain.core.models import Contact, Sale

DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'

# Field limits from the warehouse's order spec.
MAX_ADDRESS = 50
MAX_NAME = 50
MAX_POSTCODE = 15
MAX_PHONE = 30
MAX_PO_REF = 20
MAX_LINES = 50

# Placeholder staff put in the comments to show a customer was checked.
NO_INSTRUCTIONS = re.compile(r"^\[no delivery instructions\]$", re.IGNORECASE)
# Strict 24-hour window, e.g. "09:00-13:00". Loose forms ("9-1", "after 2pm")
# deliberately don't match: they stay in the text for the gate to catch.
WINDOW = re.compile(r"([01]\d|2[0-3]):([0-5]\d)\s*(?:-|to|–)\s*([01]\d|2[0-3]):([0-5]\d)")


@dataclass(frozen=True)
class EdiSettings:
    account_code: str = "DEMO"
    site_code: str = "SITE"
    default_earliest: str = "08:00"
    default_latest: str = "17:00"
    min_window_minutes: int = 120
    instructions_max_chars: int = 120
    booking_required: bool = False

    @classmethod
    def from_config(cls, warehouse: dict) -> "EdiSettings":
        delivery = warehouse.get("delivery", {})
        return cls(
            account_code=warehouse["account_code"],
            site_code=warehouse["site_code"],
            default_earliest=delivery.get("default_earliest", cls.default_earliest),
            default_latest=delivery.get("default_latest", cls.default_latest),
            min_window_minutes=delivery.get("min_window_minutes", cls.min_window_minutes),
            instructions_max_chars=delivery.get("instructions_max_chars", cls.instructions_max_chars),
            booking_required=delivery.get("booking_required", cls.booking_required),
        )


@dataclass(frozen=True)
class Window:
    instructions: str
    earliest: str
    latest: str
    source: str  # "parsed", "widened" or "default"


# ---------- field rules ----------

def uk_phone(raw: str) -> str | None:
    """Any UK format to +44 with no leading zero, or None if it isn't valid."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0044"):
        digits = digits[4:]
    elif digits.startswith("44"):
        digits = digits[2:]
    digits = digits.removeprefix("0")
    if len(digits) == 10 and digits[0] in "1237":
        return "+44" + digits
    return None


def resolve_contact(sale: Sale) -> tuple[str, str | None, str]:
    """Who the driver calls, as (name, phone, which rule chose them).

    1. A contact with "Delivery" in their job title, mobile first.
    2. The sale header contact.
    3. The default contact on the customer record.
    4. Any other contact, mobile first.
    The default contact is often the buyer or accounts, which is why it isn't first.
    """
    def mobile_first(c: Contact) -> str | None:
        return uk_phone(c.mobile) or uk_phone(c.phone)

    for c in sale.contacts:
        if "delivery" in c.job_title.lower() and (phone := mobile_first(c)):
            return c.name, phone, f"delivery tag: {c.name}"
    if phone := uk_phone(sale.phone):
        return sale.contact_name, phone, "sale header"
    for c in sale.contacts:
        if c.is_default and (phone := uk_phone(c.phone) or uk_phone(c.mobile)):
            return c.name, phone, f"default contact: {c.name}"
    for c in sale.contacts:
        if phone := mobile_first(c):
            return c.name, phone, f"fallback contact: {c.name}"
    return sale.contact_name or sale.customer_name, None, "none"


def _minutes(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:5])


def _hhmm(m: int) -> str:
    return f"{m // 60 % 24:02d}:{m % 60:02d}"


def parse_window(text: str, settings: EdiSettings) -> Window:
    """Take the last strict HH:MM-HH:MM window out of the text.

    The window is removed from the instructions, because the warehouse forbids
    times there. A window under the minimum is widened from its start rather
    than thrown away.
    """
    raw = (text or "").strip()
    matches = list(WINDOW.finditer(raw))
    if matches:
        m = matches[-1]
        earliest, latest = f"{m[1]}:{m[2]}", f"{m[3]}:{m[4]}"
        if earliest < latest:
            source = "parsed"
            if _minutes(latest) - _minutes(earliest) < settings.min_window_minutes:
                latest = _hhmm(min(_minutes(earliest) + settings.min_window_minutes, 23 * 60 + 59))
                source = "widened"
            rest = (raw[: m.start()] + raw[m.end():])
            rest = re.sub(r"\s{2,}", " ", re.sub(r"[\s,;.]+$", "", rest)).strip()
            return Window(rest, earliest, latest, source)
    return Window(raw, settings.default_earliest, settings.default_latest, "default")


def split_address(line1: str, line2: str, limit: int = MAX_ADDRESS) -> tuple[str, str, bool]:
    """Move the tail of an over-long first line into the second, at a word break."""
    a, b = (line1 or "").strip(), (line2 or "").strip()
    if len(a) <= limit:
        return a, b[:limit], False
    cut = a.rfind(" ", 0, limit + 1)
    if cut < limit * 0.5:
        cut = limit
    head, tail = a[:cut].strip(), a[cut:].strip()
    return head, (f"{tail}, {b}" if b else tail)[:limit], True


# ---------- the order ----------

def build(sale: Sale, settings: EdiSettings, today: date) -> PreparedOrder:
    address = sale.shipping_address
    addr1, addr2, address_split = split_address(address.line1, address.line2)
    town_raw = address.city.strip() or address.state.strip()
    town = town_raw[:MAX_ADDRESS]
    postcode = address.postcode[:MAX_POSTCODE]
    contact_name, phone, phone_source = resolve_contact(sale)
    window = parse_window(sale.note, settings)

    instructions = window.instructions
    has_instructions = bool(instructions) and not NO_INSTRUCTIONS.match(instructions)
    truncated = has_instructions and len(instructions) > settings.instructions_max_chars
    if truncated:
        instructions = instructions[: settings.instructions_max_chars - 3].rstrip() + "..."

    root = ET.Element("ORDERS")
    so = ET.SubElement(root, "SO")

    _section(so, "HD", [
        ("FKCustomer", settings.account_code),
        ("FKUnderBondType", "P"),  # duty paid
        ("DocumentRef", sale.so_number),
        ("DPAccountRef", postcode),  # postcode as the delivery point
        ("OrderDate", today.strftime("%d/%m/%Y")),
        ("DeliveryName", sale.customer_name[:MAX_NAME]),
        ("DeliveryAddress1", addr1),
        ("DeliveryAddress2", addr2),
        ("DeliveryTownOrCounty", town),
        ("DeliveryPostcode", postcode),
        ("DeliveryCountry", "GB"),
        ("ContactName", contact_name[:MAX_NAME]),
        ("ContactTelephoneNo", (phone or "")[:MAX_PHONE]),
    ])
    ho = [
        ("FKSalesOrderType", "1"),
        ("SiteCode", settings.site_code),
        ("EarliestTime", window.earliest),
        ("LatestTime", window.latest),
        ("IsBookingRequired", "Y" if settings.booking_required else "N"),
    ]
    # Optional fields are left out entirely when empty: sending a blank
    # instruction tells the picking system there is one.
    if has_instructions:
        ho.append(("DeliveryInstructions1", instructions))
    if sale.customer_reference:
        ho.append(("CustomerPORef", sale.customer_reference[:MAX_PO_REF]))
    ho.append(("AWRS", "RETAIL"))
    _section(so, "HO", ho)
    _section(so, "HU", [
        ("ExciseDefermentNumber", settings.account_code),
        ("CustomsDefermentNumber", settings.account_code),
        ("PayCustomsDuty", "Y"),
        ("ConsigneeName", sale.customer_name[:MAX_NAME]),
        ("ConsigneeAddress1", addr1),
        ("ConsigneeAddress2", addr2),
        ("ConsigneeAddress4", town),
        ("ConsigneePostcode", postcode),
        ("ConsigneeCountry", "GB"),
        ("BT", "2"),
    ])
    for number, line in enumerate(sale.lines, start=1):
        _section(so, "LD", [
            ("FKProduct", line.sku),
            ("SinglesOrdered", str(line.qty)),  # always single bottles
            ("SinglePrice", f"{line.price:.2f}"),
            ("PODLineNumber", str(number)),
        ])

    ET.indent(root, space="  ")
    # Empty fields as <Tag></Tag>, like the live workflow, not <Tag />: both are
    # valid XML, but this warehouse has rejected "valid" differences before.
    body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    payload = (DECLARATION + "\n" + body).encode("utf-8")

    problems = []
    if phone is None:
        problems.append(
            "No usable phone number for this customer. Add a mobile to a contact with "
            "'Delivery' in their job title, or to the default contact."
        )
    if len(re.sub(r"[^A-Za-z]", "", town_raw)) < 4:
        problems.append("The delivery town is missing or too short. The warehouse needs a valid post town.")
    if len(sale.lines) > MAX_LINES:
        problems.append(f"{len(sale.lines)} lines; the warehouse accepts at most {MAX_LINES} per order.")

    return PreparedOrder(
        doc_ref=sale.so_number,
        payload=payload,
        problems=tuple(problems),
        flags={
            "phone_source": phone_source,
            "window_source": window.source,
            "address_split": address_split,
            "instructions_truncated": truncated,
        },
    )


def _section(parent: ET.Element, tag: str, fields: list[tuple[str, str]]) -> ET.Element:
    element = ET.SubElement(parent, tag)
    for name, value in fields:
        ET.SubElement(element, name).text = value
    return element
