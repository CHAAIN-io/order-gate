"""Each warehouse answer lands in the right state, nothing is ever sent
twice, and a send never happens without a claim."""

from datetime import date

import pytest

from chaain.connectors.stock_system.generator import SaleGenerator
from chaain.connectors.warehouse.interface import PreparedOrder
from chaain.connectors.warehouse.mock import Behaviour, MockWarehouse
from chaain.core.ledger import State
from chaain.core.ledger.sqlite import SqliteLedger
from chaain.workflows.order_push.push import Outcome, push


@pytest.fixture
def sale():
    return SaleGenerator(seed=5).make()


@pytest.fixture
def ledger(sale):
    ledger = SqliteLedger()
    ledger.open(sale.so_number, sale.id, sale.customer_id, "first seen")
    return ledger


def warehouse(*script, **kwargs):
    return MockWarehouse(script=script, today=date(2026, 11, 4), **kwargs)


# behaviour, outcome, final state, did it really arrive?
CASES = [
    (Behaviour.accept(), Outcome.SENT, State.SENT, 1),
    (Behaviour.reject(), Outcome.REJECTED, State.REJECTED, 0),
    (Behaviour.auth_fail(), Outcome.AUTH_FAILED, State.REJECTED, 0),
    (Behaviour.connect_fail(), Outcome.NOT_SENT, State.RETRY, 0),
    (Behaviour.server_error(), Outcome.UNCLEAR, State.CHECK, 0),
    (Behaviour.server_error(received=True), Outcome.UNCLEAR, State.CHECK, 1),
    (Behaviour.garbled(), Outcome.UNCLEAR, State.CHECK, 1),
    (Behaviour.timeout(), Outcome.UNCLEAR, State.CHECK, 1),
    (Behaviour.dropped(), Outcome.UNCLEAR, State.CHECK, 1),
    (Behaviour.slow(45), Outcome.SENT, State.SENT, 1),
    (Behaviour.slow(90), Outcome.UNCLEAR, State.CHECK, 1),
]


@pytest.mark.parametrize("behaviour, outcome, state, arrived", CASES)
def test_each_answer_lands_in_the_right_state_after_one_attempt(sale, ledger, behaviour, outcome, state, arrived):
    wh = warehouse(behaviour)
    result = push(sale, ledger, wh)

    assert (result.outcome, result.state) == (outcome, state)
    assert ledger.find(so_number=sale.so_number).state is state
    assert wh.attempts == [sale.so_number]  # one attempt, never retried
    assert wh.received[sale.so_number] == arrived


def test_a_timeout_that_arrived_is_never_sent_again(sale, ledger):
    wh = warehouse(Behaviour.timeout())
    push(sale, ledger, wh)
    second = push(sale, ledger, wh)  # the next run sees the same sale

    assert not second.attempted and "claim failed" in second.reason
    assert wh.received[sale.so_number] == 1


def test_auth_failure_stops_the_run(sale, ledger):
    assert push(sale, ledger, warehouse(Behaviour.auth_fail())).stop_run


def test_every_outcome_is_recorded_with_its_evidence(sale, ledger):
    push(sale, ledger, warehouse(Behaviour.timeout()))
    last = ledger.history(sale.so_number)[-1]
    assert last.to_state is State.CHECK and "may have arrived" in last.reason
    assert last.details["transport"] == "timeout" and last.details["window_source"]


def test_no_claim_means_no_send(sale):
    wh = warehouse()
    result = push(sale, SqliteLedger(), wh)  # no row to claim
    assert not result.attempted and wh.attempts == []


def test_ledger_write_fails_after_200_goes_to_check_and_is_not_resent(sale, ledger, monkeypatch):
    real_move = ledger.move
    def fail_on_sent(so, to, reason, *args, **kwargs):
        if to is State.SENT:
            raise RuntimeError("database is locked")
        return real_move(so, to, reason, *args, **kwargs)
    monkeypatch.setattr(ledger, "move", fail_on_sent)
    wh = warehouse(Behaviour.accept())

    result = push(sale, ledger, wh)
    assert (result.state, result.recorded) == (State.CHECK, False)
    assert "a person must confirm" in ledger.find(so_number=sale.so_number).reason

    push(sale, ledger, wh)
    assert wh.received[sale.so_number] == 1


def test_if_every_write_fails_the_claim_still_blocks_a_resend(sale, ledger, monkeypatch):
    real_move = ledger.move
    def fail_after_claim(so, to, reason, *args, **kwargs):
        if to is not State.PENDING:
            raise RuntimeError("disk full")
        return real_move(so, to, reason, *args, **kwargs)
    monkeypatch.setattr(ledger, "move", fail_after_claim)
    wh = warehouse(Behaviour.accept())

    result = push(sale, ledger, wh)
    assert result.state is State.PENDING and result.stop_run
    push(sale, ledger, wh)
    assert wh.received[sale.so_number] == 1


def test_known_problems_are_not_claimed_or_sent():
    sale = SaleGenerator(seed=5).make("no_phone")
    ledger = SqliteLedger()
    ledger.open(sale.so_number, sale.id, sale.customer_id, "first seen")
    wh = warehouse()

    result = push(sale, ledger, wh)
    assert not result.attempted and "phone" in result.reason
    assert ledger.find(so_number=sale.so_number).state is State.CANDIDATE


def test_mock_rejects_the_declaration_the_live_warehouse_refused(sale, ledger):
    wh = warehouse()
    good = wh.prepare(sale)
    bad = PreparedOrder(good.doc_ref, good.payload.replace(
        b'<?xml version="1.0" encoding="UTF-8"?>', b"<?xml version='1.0' encoding='utf-8'?>"
    ), ())
    result = wh.send(bad)
    assert result.status == 400 and wh.received[sale.so_number] == 0


def test_lookup_is_off_unless_enabled(sale, ledger):
    off = warehouse(Behaviour.timeout())
    push(sale, ledger, off)
    assert off.lookup(sale.so_number) is None  # a person has to check

    on = warehouse(Behaviour.timeout(), lookup_enabled=True)
    on.received[sale.so_number] += 1
    assert on.lookup(sale.so_number) is True
    assert on.lookup("SO-99999") is False
