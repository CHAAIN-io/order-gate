"""The ledger records every decision once, refuses moves the state machine
doesn't allow, and never leaves a half-written change behind."""

from datetime import datetime, timedelta, timezone

import pytest

from chaain.core.ledger import (
    ALLOWED, DuplicateEntry, InvalidTransition, Ledger, NotFound, State, can_move,
)
from chaain.core.ledger.sqlite import SqliteLedger

GUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def ledger():
    return SqliteLedger(clock=Clock())


@pytest.fixture
def opened(ledger):
    ledger.open("SO-10001", GUID, "cust-1", "first seen in poll")
    return ledger


# ---------- the state machine ----------

def test_every_state_has_a_rule_and_sent_is_the_end():
    assert set(ALLOWED) == set(State)
    assert ALLOWED[State.SENT] == frozenset()


def test_nothing_goes_back_to_pending_without_a_person_saying_it_did_not_arrive():
    # The only routes into PENDING start from states where the order is
    # known not to be at the warehouse.
    sources = {s for s, targets in ALLOWED.items() if State.PENDING in targets}
    assert sources == {State.CANDIDATE, State.SKIPPED, State.HELD, State.RETRY}


def test_final_skip_never_moves():
    assert can_move(State.SKIPPED, State.PENDING)
    assert not can_move(State.SKIPPED, State.PENDING, final=True)


# ---------- recording ----------

def test_open_records_candidate_with_reason_and_time(opened):
    entry = opened.find(so_number="SO-10001")
    assert entry.state is State.CANDIDATE
    [event] = opened.history("SO-10001")
    assert (event.from_state, event.to_state, event.reason) == (None, State.CANDIDATE, "first seen in poll")


def test_happy_path_claim_then_sent(opened):
    opened.claim("SO-10001", "gate: AUTO", fingerprint="abc")
    opened.move("SO-10001", State.SENT, "warehouse returned 200", details={"status": 200})
    entry = opened.find(guid=GUID)
    assert entry.state is State.SENT and entry.fingerprint == "abc"
    assert [e.to_state for e in opened.history("SO-10001")] == [
        State.CANDIDATE, State.PENDING, State.SENT,
    ]
    assert opened.history("SO-10001")[-1].details == {"status": 200}


def test_unclear_outcome_is_resolved_by_a_person(opened):
    opened.claim("SO-10001", "gate: AUTO")
    opened.move("SO-10001", State.CHECK, "timed out after 60s")
    opened.move("SO-10001", State.RETRY, "not found at warehouse", actor="Ops lead")
    opened.move("SO-10001", State.PENDING, "gate: AUTO")
    retry = opened.history("SO-10001")[3]
    assert (retry.to_state, retry.actor) == (State.RETRY, "Ops lead")


def test_rejected_can_be_retried_once_fixed(opened):
    opened.claim("SO-10001", "gate: AUTO")
    opened.move("SO-10001", State.REJECTED, "400: invalid postcode")
    opened.move("SO-10001", State.RETRY, "postcode fixed in stock system", actor="Ops lead")
    assert opened.find(so_number="SO-10001").state is State.RETRY


@pytest.mark.parametrize("path", [
    [State.SENT],                                   # can't send without a claim
    [State.PENDING, State.SENT, State.PENDING],     # can't resend
    [State.PENDING, State.CHECK, State.PENDING],    # can't resend a maybe
    [State.PENDING, State.PENDING],                 # can't claim twice
])
def test_disallowed_moves_are_refused(opened, path):
    with pytest.raises(InvalidTransition):
        for state in path:
            opened.move("SO-10001", state, "test")


def test_final_skip_is_permanent_but_location_skip_is_rechecked(opened, ledger):
    opened.move("SO-10001", State.SKIPPED, "#NOVISION in note", final=True)
    with pytest.raises(InvalidTransition, match="final skip"):
        opened.move("SO-10001", State.PENDING, "marker removed")

    ledger.open("SO-10002", None, "cust-2", "first seen")
    ledger.move("SO-10002", State.SKIPPED, "Shopify, picked at Site-B")
    ledger.move("SO-10002", State.HELD, "pick switched to warehouse: send by hand?")
    assert ledger.find(so_number="SO-10002").state is State.HELD


def test_every_change_needs_a_reason(opened):
    with pytest.raises(ValueError):
        opened.move("SO-10001", State.PENDING, "  ")


def test_moving_an_unknown_order_fails(ledger):
    with pytest.raises(NotFound):
        ledger.move("SO-99999", State.PENDING, "test")


# ---------- uniqueness ----------

@pytest.mark.parametrize("so, guid", [
    ("SO-10001", "11111111-1111-4111-8111-111111111111"),  # same SO number
    ("so-10001 ", None),                                    # same SO, different case and spacing
    ("SO-20002", GUID.upper()),                             # same GUID, different case
])
def test_one_row_per_sale(opened, so, guid):
    with pytest.raises(DuplicateEntry):
        opened.open(so, guid, "cust-1", "seen again")


def test_hand_entered_row_with_truncated_guid_still_blocks_by_so_number(ledger):
    ledger.record_manual("SO-10001", GUID[:13], actor="Ops lead", reason="keyed into warehouse by hand")
    entry = ledger.find(guid=GUID, so_number="SO-10001")
    assert entry is not None and entry.state is State.SENT and entry.source == "manual"
    with pytest.raises(DuplicateEntry):
        ledger.open("SO-10001", GUID, "cust-1", "first seen in poll")


# ---------- transactions ----------

def test_a_failed_write_leaves_nothing_half_done(opened, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(opened, "_insert_event", broken)

    with pytest.raises(RuntimeError):
        opened.claim("SO-10001", "gate: AUTO")

    # The state change was rolled back with the failed history write.
    assert opened.find(so_number="SO-10001").state is State.CANDIDATE
    assert len(opened.history("SO-10001")) == 1


def test_second_claimer_on_the_same_file_is_refused(tmp_path):
    path = tmp_path / "ledger.db"
    first, second = SqliteLedger(path), SqliteLedger(path)
    first.open("SO-10001", GUID, "cust-1", "first seen")

    first.claim("SO-10001", "run A")
    with pytest.raises(InvalidTransition):
        second.claim("SO-10001", "run B")
    assert [e.reason for e in second.history("SO-10001")] == ["first seen", "run A"]


def test_survives_a_restart(tmp_path):
    path = tmp_path / "ledger.db"
    SqliteLedger(path).open("SO-10001", GUID, "cust-1", "first seen")
    assert SqliteLedger(path).find(so_number="SO-10001").state is State.CANDIDATE


def test_in_state_lists_what_needs_attention(ledger):
    for n in (1, 2, 3):
        ledger.open(f"SO-1000{n}", None, "c", "seen")
        ledger.claim(f"SO-1000{n}", "AUTO")
    ledger.move("SO-10002", State.CHECK, "timeout")
    assert [e.so_number for e in ledger.in_state(State.CHECK, State.PENDING)] == [
        "SO-10001", "SO-10003", "SO-10002",
    ]


def test_sqlite_ledger_is_a_ledger():
    assert isinstance(SqliteLedger(), Ledger)
