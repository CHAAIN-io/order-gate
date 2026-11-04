"""The demo day: repeatable, offline, and every past incident lands where it should."""

import pytest

from chaain.connectors.notifier.mock import MemoryNotifier
from chaain.connectors.stock_system.generator import SaleGenerator
from chaain.connectors.warehouse.mock import Behaviour, MockWarehouse
from chaain.core.ledger import State
from chaain.llm import LLMError
from chaain.llm.replay import ReplayProvider
from chaain.workflows.order_push import demo
from chaain.workflows.order_push.demo import ORDERS, play

# Where each order should be at 13:30, by its place in ORDERS. None: no row (ignored).
EXPECTED = (
    State.SENT, State.SKIPPED, State.SENT, State.HELD, State.SENT,
    State.HELD, State.SKIPPED, State.REJECTED, State.HELD, None,
    State.SENT, State.HELD, State.CHECK, State.SENT, State.SKIPPED,
    State.HELD, State.HELD, State.HELD, State.SENT, State.SENT,
)


@pytest.fixture(scope="module")
def day():
    lines = []
    ws = play(out=lines.append, notifier=MemoryNotifier())
    return ws, lines


def so(i):
    return f"SO-{10001 + i:05d}"


def test_the_day_is_the_same_every_time(day):
    _, lines = day
    again = []
    play(out=again.append, notifier=MemoryNotifier())
    assert again == lines


def test_every_order_ends_where_the_story_says(day):
    ws, _ = day
    assert len(ORDERS) == len(EXPECTED) == 20
    for i, expected in enumerate(EXPECTED):
        entry = ws.ledger.find(so_number=so(i))
        assert (entry.state if entry else None) is expected, f"{so(i)}: {ORDERS[i].story}"


def test_nothing_reaches_the_warehouse_twice(day):
    ws, _ = day
    assert ws.warehouse.received and max(ws.warehouse.received.values()) == 1
    assert ws.warehouse.received[so(12)] == 1  # the timeout did arrive: CHECK, not resent


def test_the_ai_holds_come_from_recorded_answers_and_its_quiet_note_goes(day):
    ws, lines = day
    for i in (8, 15, 17):
        assert "replay/" in ws.ledger.find(so_number=so(i)).reason
    assert ws.ledger.find(so_number=so(2)).state is State.SENT  # "as per usual" adds nothing
    assert "Live model calls: 0." in "\n".join(lines)


def test_each_new_item_is_alerted_once(day):
    ws, _ = day
    bodies = "\n".join(body for _, body in ws.notifier.sent)
    for i, expected in enumerate(EXPECTED):
        if expected in (State.HELD, State.REJECTED, State.CHECK):
            assert bodies.count(f"- {so(i)} ") == 1


def test_the_cut_offs_are_called_out(day):
    _, lines = day
    text = "\n".join(lines)
    assert "goes today" in text and "goes tomorrow" in text


def test_the_ignored_order_is_said_once(day):
    _, lines = day
    assert sum(so(9) in line and "ignored" in line for line in lines) == 1


def test_a_note_with_no_recorded_answer_holds():
    read = demo._replayed_notes({"before-10am": "Deliver before 10am please, kitchen closed after."})
    sale = SaleGenerator(seed=1).make("standard", note="Something nobody recorded")
    result = read(sale)
    assert result.error == "no recorded answer for this text"


def test_a_recording_older_than_the_eval_case_is_refused():
    with pytest.raises(ValueError, match="re-run the eval"):
        demo._replayed_notes({"before-10am": "the wording has changed"})


def test_the_replay_provider_answers_only_what_was_recorded():
    provider = ReplayProvider({"known": {"flags": {}}}, model="m")
    assert provider.complete_json("", "known", {}) == {"flags": {}}
    with pytest.raises(LLMError):
        provider.complete_json("", "unknown", {})


def test_a_planned_answer_is_used_for_its_order_and_only_once():
    gen = SaleGenerator(seed=1)
    a, b = gen.make(), gen.make()
    wh = MockWarehouse(planned={b.so_number: Behaviour.reject()})
    assert wh.send(wh.prepare(a)).status == 200
    assert wh.send(wh.prepare(b)).status == 400
    assert wh.send(wh.prepare(b)).status == 200
