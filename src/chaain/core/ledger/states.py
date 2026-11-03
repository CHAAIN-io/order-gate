"""The ledger's states and the only moves allowed between them.

    (first seen) ─► CANDIDATE ─► SKIPPED          (final for #NOVISION,
                        │                          credited, voided)
                        ├──────► HELD ──approve──► PENDING
                        │          └──reject───► SKIPPED
                        └─AUTO + claim──► PENDING
                                            ├─ accepted ───────► SENT (final)
                                            ├─ refused ────────► REJECTED ─fixed─► RETRY
                                            ├─ never left us ──► RETRY
                                            └─ unclear/stale ──► CHECK
                                                                   ├─ it arrived ─► SENT
                                                                   └─ it didn't ──► RETRY
    RETRY and non-final SKIPPED go through the gate again: PENDING, HELD or SKIPPED.
    Orders entered by hand are recorded straight into SENT.

There is no way back to PENDING from SENT or CHECK. That is what makes the
system at-most-once: nothing reaches the warehouse twice without a person
first saying it didn't arrive.
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    CANDIDATE = "CANDIDATE"
    SKIPPED = "SKIPPED"
    HELD = "HELD"
    PENDING = "PENDING"  # claimed: we are about to send, or did and don't know yet
    SENT = "SENT"  # at the warehouse
    REJECTED = "REJECTED"  # the warehouse refused it, so it is not there
    CHECK = "CHECK"  # may or may not be at the warehouse: a person must look
    RETRY = "RETRY"  # confirmed not at the warehouse; may be sent again


_REGATE = frozenset({State.PENDING, State.HELD, State.SKIPPED})

ALLOWED: dict[State, frozenset[State]] = {
    State.CANDIDATE: _REGATE,
    State.SKIPPED: _REGATE,  # only when the skip isn't final
    State.HELD: frozenset({State.PENDING, State.SKIPPED}),
    State.PENDING: frozenset({State.SENT, State.REJECTED, State.CHECK, State.RETRY}),
    State.SENT: frozenset(),
    State.REJECTED: frozenset({State.RETRY}),
    State.CHECK: frozenset({State.SENT, State.RETRY}),
    State.RETRY: _REGATE,
}


def can_move(current: State, to: State, final: bool = False) -> bool:
    """Whether `current` may become `to`. A final skip can never move."""
    if current is State.SKIPPED and final:
        return False
    return to in ALLOWED[current]
