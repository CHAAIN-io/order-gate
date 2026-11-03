"""Send one order to the warehouse, at most once.

    prepare -> claim (PENDING) -> send once -> classify -> record

Never retries. If anything is unclear, the order goes to CHECK and a person
decides. If recording the outcome fails, we try to record CHECK instead; if
even that fails, the row stays PENDING, which still blocks a resend, and the
resolver flags it once it goes stale.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from chaain.connectors.warehouse.interface import SendResult, Transport, Warehouse
from chaain.core.ledger import Ledger, State
from chaain.core.models import Sale


class Outcome(str, Enum):
    SENT = "sent"  # accepted: it's at the warehouse
    REJECTED = "rejected"  # refused: it's not
    AUTH_FAILED = "auth_failed"  # refused for credentials: every send will fail
    NOT_SENT = "not_sent"  # never connected: it can't be there
    UNCLEAR = "unclear"  # it may or may not be there


# Where each outcome leaves the ledger row.
TARGET = {
    Outcome.SENT: State.SENT,
    Outcome.REJECTED: State.REJECTED,
    Outcome.AUTH_FAILED: State.REJECTED,
    Outcome.NOT_SENT: State.RETRY,
    Outcome.UNCLEAR: State.CHECK,
}


def classify(result: SendResult) -> Outcome:
    """What a send result means. Anything not clearly yes or clearly no is UNCLEAR.

    The live workflow treated a 500 or an odd 200 as "rejected" and told the
    team to enter the order by hand, which duplicates it if it did arrive.
    """
    if result.transport is Transport.CONNECT_FAILED:
        return Outcome.NOT_SENT
    if result.transport is not Transport.RESPONSE:
        return Outcome.UNCLEAR  # timeout or dropped connection
    status = result.status or 0
    if status == 200:
        # The warehouse confirms with "saved"; any other 200 might be a proxy page.
        return Outcome.SENT if "saved" in result.body.lower() else Outcome.UNCLEAR
    if status in (401, 403):
        return Outcome.AUTH_FAILED
    if status == 429:
        return Outcome.NOT_SENT  # rate-limited before it was processed
    if status == 408:
        return Outcome.UNCLEAR  # the server gave up waiting; it may have processed it
    if 400 <= status < 500:
        return Outcome.REJECTED
    return Outcome.UNCLEAR  # 5xx and anything unexpected


@dataclass(frozen=True)
class PushResult:
    so_number: str
    attempted: bool  # whether a send was made
    outcome: Outcome | None
    state: State | None  # where the ledger row ended up, if we know
    recorded: bool  # whether the outcome itself made it into the ledger
    stop_run: bool  # stop sending anything else this run
    reason: str


def fingerprint(sale: Sale) -> str:
    """A short hash of what the warehouse acts on, to spot changes after sending."""
    content = {
        "status": sale.status.value,
        "address": [sale.shipping_address.line1, sale.shipping_address.line2,
                    sale.shipping_address.city, sale.shipping_address.postcode],
        "note": sale.note,
        "memo": sale.memo,
        "lines": [[l.sku, l.qty, str(l.price), l.pick_location] for l in sale.lines],
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]


def push(sale: Sale, ledger: Ledger, warehouse: Warehouse) -> PushResult:
    so = sale.so_number
    order = warehouse.prepare(sale)
    if order.problems:
        # The gate should already have held this; refuse rather than send a known reject.
        return PushResult(so, False, None, None, False, False, "not sent: " + " ".join(order.problems))

    try:
        ledger.claim(so, "claimed for sending", fingerprint=fingerprint(sale))
    except Exception as e:  # no claim, no send
        return PushResult(so, False, None, None, False, False, f"claim failed, not sent: {e}")

    result = warehouse.send(order)
    outcome = classify(result)
    details = {
        "transport": result.transport.value, "status": result.status,
        "body": result.body[:500], "elapsed_seconds": result.elapsed_seconds, **order.flags,
    }
    reason = _describe(outcome, result)
    stop = outcome is Outcome.AUTH_FAILED

    try:
        ledger.move(so, TARGET[outcome], reason, details=details)
        return PushResult(so, True, outcome, TARGET[outcome], True, stop, reason)
    except Exception as first:
        # We know what the warehouse said but couldn't record it: the 14 Oct case.
        # CHECK says "a person must confirm", which is true whatever happened.
        fallback = f"{reason}; recording that failed ({first}), so a person must confirm"
        try:
            ledger.move(so, State.CHECK, fallback, details=details)
            return PushResult(so, True, outcome, State.CHECK, False, stop, fallback)
        except Exception as second:
            return PushResult(
                so, True, outcome, State.PENDING, False, True,
                f"{reason}; recording failed twice ({second}). Row left at PENDING, "
                "which blocks a resend; the resolver will flag it.",
            )


def _describe(outcome: Outcome, result: SendResult) -> str:
    return {
        Outcome.SENT: "accepted by the warehouse",
        Outcome.REJECTED: f"refused by the warehouse ({result.status}): {result.body[:200]}",
        Outcome.AUTH_FAILED: f"refused for credentials ({result.status}); sending stopped for this run",
        Outcome.NOT_SENT: "couldn't reach the warehouse; nothing was sent, safe to try next run",
        Outcome.UNCLEAR: _unclear(result),
    }[outcome]


def _unclear(result: SendResult) -> str:
    if result.transport is Transport.TIMEOUT:
        return f"no answer within {result.elapsed_seconds:.0f}s; it may have arrived"
    if result.transport is Transport.DROPPED:
        return "connection dropped mid-send; it may have arrived"
    return f"unclear answer ({result.status}): {result.body[:200]}; it may have arrived"
