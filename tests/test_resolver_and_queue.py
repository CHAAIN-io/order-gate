"""The resolver finds what's stuck, even on a quiet weekend, and asks a
person. The team queue shows it in risk order, and every action is logged
with who did it. Nothing here guesses whether the warehouse has an order."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from chaain.connectors.notifier.mock import MemoryNotifier
from chaain.connectors.stock_system.generator import SaleGenerator
from chaain.connectors.stock_system.mock import MockStockSystem
from chaain.connectors.warehouse.mock import Behaviour, MockWarehouse
from chaain.core.decision import Decision, Route
from chaain.core.ledger import State
from chaain.core.ledger.sqlite import SqliteLedger
from chaain.workflows.order_push.queue import (
    CHANGED, HELD, REFUSED, UNKNOWN, Cutoffs, QueueError, approve, build_queue, mark_received,
    mark_retry, reject,
)
from chaain.workflows.order_push.resolver import (
    ALERTED, NOT_FOUND, ResolverSettings, resolve, resolver_schedule,
)
from chaain.workflows.order_push.run import RunSettings, run_once

UK = ZoneInfo("Europe/London")
FRIDAY = datetime(2026, 11, 6, 17, 50, tzinfo=UK)
OPS = "Ops lead"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)
        return self.now


def send_everything(sale):
    return Decision(Route.AUTO, ())


def hold_everything(sale):
    return Decision(Route.HOLD, ("Credit hold on the stock system", "New customer: check the delivery contact"))


@pytest.fixture
def clock():
    return Clock(FRIDAY)


@pytest.fixture
def ledger(clock):
    return SqliteLedger(clock=clock)


@pytest.fixture
def gen():
    return SaleGenerator(seed=5, now=FRIDAY)


@pytest.fixture
def stock():
    return MockStockSystem()


@pytest.fixture
def wh():
    return MockWarehouse(today=FRIDAY.date())


def claimed(ledger, stock, sale):
    """A sale claimed for sending whose outcome was never recorded."""
    stock.add(sale)
    ledger.open(sale.so_number, sale.id, sale.customer_id, "first seen in poll")
    ledger.claim(sale.so_number, "claimed for sending")


def checked(ledger, stock, sale, reason="no answer within 60s; it may have arrived"):
    claimed(ledger, stock, sale)
    ledger.move(sale.so_number, State.CHECK, reason, details={"transport": "timeout", "elapsed_seconds": 60})


def held(ledger, stock, wh, sale, gate=hold_everything, now=FRIDAY):
    stock.add(sale)
    run_once(now, stock, ledger, wh, gate)
    assert ledger.find(so_number=sale.so_number).state is State.HELD


# ---------- Test 12: the quiet weekend ----------

def test_12_stale_pending_is_found_on_a_quiet_weekend_and_resent_after_a_person_checks(
    gen, clock, ledger, stock, wh,
):
    """Friday 17:50: an order is claimed, then the run dies before sending.
    No more orders arrive all weekend. The live stale check ran only when a
    poll found recent sales, so nobody would hear until Monday."""
    sale = gen.make()
    claimed(ledger, stock, sale)
    notifier = MemoryNotifier()
    schedule = resolver_schedule(ResolverSettings())

    # The resolver keeps running through the night and the weekend.
    assert schedule.is_due(datetime(2026, 11, 7, 3, 0, tzinfo=UK))  # Saturday 03:00
    assert schedule.is_due(datetime(2026, 11, 8, 14, 15, tzinfo=UK))  # Sunday afternoon

    resolve(clock.advance(minutes=15), ledger, wh, notifier)  # 18:05: a send may still be running
    assert ledger.find(so_number=sale.so_number).state is State.PENDING and not notifier.sent

    report = resolve(clock.advance(minutes=15), ledger, wh, notifier)  # 18:20
    assert report.made_check == [sale.so_number]
    assert ledger.find(so_number=sale.so_number).state is State.CHECK
    assert len(notifier.sent) == 1 and sale.so_number in notifier.sent[0][1]

    for _ in range(4 * 24 * 2):  # every 15 minutes until Monday: no repeat alerts
        resolve(clock.advance(minutes=15), ledger, wh, notifier)
    assert len(notifier.sent) == 1

    # Monday 09:00: the Ops lead searches the warehouse and it isn't there.
    clock.now = datetime(2026, 11, 9, 9, 0, tzinfo=UK)
    mark_retry(sale.so_number, OPS, ledger=ledger, note="searched by doc ref, not found")
    # The sale last changed on Friday, outside the 48-hour lookback, but it still goes.
    run_once(clock.now, stock, ledger, wh, send_everything, RunSettings(lookback_hours=48))
    assert ledger.find(so_number=sale.so_number).state is State.SENT
    assert wh.received[sale.so_number] == 1

    actors = [e.actor for e in ledger.history(sale.so_number)]
    assert "resolver" in actors and OPS in actors


# ---------- the resolver ----------

def test_lookup_found_marks_sent_but_not_found_stays_with_a_person(gen, ledger, stock):
    wh = MockWarehouse(today=FRIDAY.date(), lookup_enabled=True)
    there, missing = gen.make(), gen.make()
    checked(ledger, stock, there)
    checked(ledger, stock, missing)
    wh.received[there.so_number] = 1
    settings = ResolverSettings(lookup_enabled=True)

    report = resolve(FRIDAY, ledger, wh, MemoryNotifier(), settings)
    resolve(FRIDAY, ledger, wh, MemoryNotifier(), settings)  # a second run doesn't repeat the note

    assert report.confirmed_sent == [there.so_number] and report.not_found == [missing.so_number]
    assert ledger.find(so_number=there.so_number).state is State.SENT
    assert ledger.find(so_number=missing.so_number).state is State.CHECK
    notes = [e for e in ledger.history(missing.so_number) if e.reason == NOT_FOUND]
    assert len(notes) == 1
    [item] = build_queue(FRIDAY, ledger, stock)
    assert any(NOT_FOUND in line for line in item.evidence)


def test_lookup_is_not_used_when_switched_off(gen, ledger, stock):
    wh = MockWarehouse(today=FRIDAY.date(), lookup_enabled=True)
    sale = gen.make()
    checked(ledger, stock, sale)
    wh.received[sale.so_number] = 1
    resolve(FRIDAY, ledger, wh, MemoryNotifier(), ResolverSettings(lookup_enabled=False))
    assert ledger.find(so_number=sale.so_number).state is State.CHECK


def test_a_failed_alert_is_tried_again_next_run(gen, ledger, stock):
    sale = gen.make()
    checked(ledger, stock, sale)
    notifier = MemoryNotifier(fail=True)
    report = resolve(FRIDAY, ledger, MockWarehouse(), notifier)
    assert report.alert_failed and not any(e.reason == ALERTED for e in ledger.history(sale.so_number))
    notifier.fail = False
    assert resolve(FRIDAY, ledger, MockWarehouse(), notifier).alerted == [sale.so_number]


def test_an_item_is_alerted_again_when_it_comes_back(gen, clock, ledger, stock):
    sale = gen.make()
    checked(ledger, stock, sale)
    notifier = MemoryNotifier()
    resolve(clock.now, ledger, MockWarehouse(), notifier)
    mark_retry(sale.so_number, OPS, ledger=ledger)
    run_once(clock.advance(minutes=5), stock, ledger, MockWarehouse(script=[Behaviour.timeout()]), send_everything)
    assert ledger.find(so_number=sale.so_number).state is State.CHECK
    resolve(clock.now, ledger, MockWarehouse(), notifier)
    assert len(notifier.sent) == 2


# ---------- the queue ----------

def test_queue_order_is_unknown_first_then_held_then_refused_oldest_first(gen, clock, ledger, stock, wh):
    refused = gen.make()
    stock.add(refused)
    run_once(clock.now, stock, ledger, MockWarehouse(script=[Behaviour.reject()]), send_everything)
    first_hold, second_hold = gen.make(), gen.make()
    held(ledger, stock, wh, first_hold, now=clock.advance(minutes=1))
    held(ledger, stock, wh, second_hold, now=clock.advance(minutes=1))
    clock.advance(minutes=1)
    check, stale = gen.make(), gen.make()
    checked(ledger, stock, check)
    clock.advance(minutes=1)
    claimed(ledger, stock, stale)
    clock.advance(minutes=40)
    fresh = gen.make()
    claimed(ledger, stock, fresh)  # 0 minutes old: a send may still be running

    order = [(i.so_number, i.section) for i in build_queue(clock.now, ledger, stock)]
    assert order == [
        (check.so_number, UNKNOWN), (stale.so_number, UNKNOWN),
        (first_hold.so_number, HELD), (second_hold.so_number, HELD),
        (refused.so_number, REFUSED),
    ]


def test_a_pending_claim_inside_the_stale_limit_is_not_in_the_queue(gen, clock, ledger, stock):
    sale = gen.make()
    claimed(ledger, stock, sale)
    assert build_queue(clock.advance(minutes=10), ledger, stock) == []
    [item] = build_queue(clock.advance(minutes=25), ledger, stock)
    assert item.state is State.PENDING and item.actions == ()
    assert item.question.startswith("The resolver moves this to CHECK")


def test_a_held_item_shows_each_reason_separately(gen, ledger, stock, wh):
    sale = gen.make()
    held(ledger, stock, wh, sale)
    [item] = build_queue(FRIDAY, ledger, stock)
    assert item.reasons == ("Credit hold on the stock system", "New customer: check the delivery contact")
    assert item.customer == sale.customer_name and item.actions == ("approve", "reject")


def test_a_check_item_shows_what_the_warehouse_said_and_asks_a_plain_question(gen, ledger, stock):
    sale = gen.make()
    checked(ledger, stock, sale)
    [item] = build_queue(FRIDAY, ledger, stock)
    assert any("no answer within 60s" in line and "after 60s" in line for line in item.evidence)
    assert f"doc ref {sale.so_number}" in item.question
    assert item.actions == ("received", "retry")


# ---------- actions ----------

def test_approving_an_unchanged_hold_sends_it_and_records_who(gen, ledger, stock, wh):
    sale = gen.make()
    held(ledger, stock, wh, sale)
    message = approve(sale.so_number, OPS, ledger=ledger, stock=stock, warehouse=wh, gate=hold_everything)
    assert "SENT" in message and wh.received[sale.so_number] == 1
    claim = next(e for e in ledger.history(sale.so_number) if e.to_state is State.PENDING)
    assert claim.actor == OPS and claim.reason == f"approved by {OPS}"


def test_approving_a_hold_that_changed_shows_it_again_instead_of_sending(gen, clock, ledger, stock, wh):
    sale = gen.make()
    held(ledger, stock, wh, sale)
    stock.update(sale.id, clock.advance(minutes=5), note="Deliver to the back door this week only")

    def new_reasons(s):
        return Decision(Route.HOLD, ("Note: one off delivery change",))

    message = approve(sale.so_number, OPS, ledger=ledger, stock=stock, warehouse=wh, gate=new_reasons)
    assert "not sent" in message and wh.attempts == []
    entry = ledger.find(so_number=sale.so_number)
    assert entry.state is State.HELD
    [item] = build_queue(clock.now, ledger, stock)
    assert item.reasons == ("Note: one off delivery change",)
    assert any(CHANGED in line for line in item.evidence)

    # Having seen the new version, approving again sends it.
    approve(sale.so_number, OPS, ledger=ledger, stock=stock, warehouse=wh, gate=new_reasons)
    assert wh.received[sale.so_number] == 1


def test_a_hold_that_changed_into_a_skip_is_skipped_not_sent(gen, clock, ledger, stock, wh):
    sale = gen.make()
    held(ledger, stock, wh, sale)
    stock.update(sale.id, clock.advance(minutes=5), note="#NOVISION re-bill")

    def skip(s):
        return Decision(Route.SKIP, ("#NOVISION in the note",), final=True)

    approve(sale.so_number, OPS, ledger=ledger, stock=stock, warehouse=wh, gate=skip)
    entry = ledger.find(so_number=sale.so_number)
    assert entry.state is State.SKIPPED and entry.final and wh.attempts == []


def test_a_reject_needs_a_reason_and_is_final_even_after_a_payment_sync(gen, clock, ledger, stock, wh):
    sale = gen.make()
    held(ledger, stock, wh, sale)
    with pytest.raises(QueueError, match="say why"):
        reject(sale.so_number, OPS, "  ", ledger=ledger)
    reject(sale.so_number, OPS, "customer cancelled by phone", ledger=ledger)

    stock.update(sale.id, clock.advance(hours=2))  # a payment sync bumps last_modified
    run_once(clock.now, stock, ledger, wh, send_everything)
    entry = ledger.find(so_number=sale.so_number)
    assert entry.state is State.SKIPPED and entry.final and wh.attempts == []
    assert entry.reason == f"rejected by {OPS}: customer cancelled by phone"


def test_mark_received_settles_it_and_it_is_never_resent(gen, clock, ledger, stock, wh):
    sale = gen.make()
    checked(ledger, stock, sale)
    mark_received(sale.so_number, OPS, ledger=ledger, note="on the warehouse's pick list")
    stock.update(sale.id, clock.advance(hours=1))
    run_once(clock.now, stock, ledger, wh, send_everything)
    assert ledger.find(so_number=sale.so_number).state is State.SENT and wh.attempts == []


def test_a_refused_order_fixed_at_source_can_be_retried(gen, clock, ledger, stock):
    sale = gen.make()
    stock.add(sale)
    run_once(clock.now, stock, ledger, MockWarehouse(script=[Behaviour.reject()]), send_everything)
    mark_retry(sale.so_number, OPS, ledger=ledger, note="postcode corrected")
    wh = MockWarehouse()
    run_once(clock.advance(minutes=15), stock, ledger, wh, send_everything)
    assert wh.received[sale.so_number] == 1
    assert "fixed at source" in next(e.reason for e in ledger.history(sale.so_number) if e.to_state is State.RETRY)


def test_every_action_needs_a_name(gen, ledger, stock):
    sale = gen.make()
    checked(ledger, stock, sale)
    with pytest.raises(QueueError, match="needs a name"):
        mark_received(sale.so_number, " ", ledger=ledger)


def test_an_action_on_the_wrong_state_changes_nothing(gen, ledger, stock, wh):
    sale = gen.make()
    held(ledger, stock, wh, sale)
    approve(sale.so_number, OPS, ledger=ledger, stock=stock, warehouse=wh, gate=hold_everything)
    with pytest.raises(QueueError, match="someone may have acted on it already"):
        approve(sale.so_number, "Finance lead", ledger=ledger, stock=stock, warehouse=wh, gate=hold_everything)
    assert wh.received[sale.so_number] == 1


# ---------- cut-offs ----------

@pytest.mark.parametrize("hhmm, expected", [
    ("11:00", "1h 30m to the 12:30 cut-off"),
    ("12:45", "warehouse still accepts until 13:00 (15m left)"),
    ("13:05", "anything sent now goes tomorrow"),
])
def test_the_cut_off_counts_down_to_what_customers_are_told(hhmm, expected):
    cutoffs = Cutoffs.from_config({
        "queue": {"customer_cutoff": "12:30", "warehouse_cutoff": "13:00"},
        "scheduler": {"timezone": "Europe/London"},
    })
    h, m = map(int, hhmm.split(":"))
    assert expected in cutoffs.describe(datetime(2026, 11, 4, h, m, tzinfo=UK))


# ---------- the ledger's notes ----------

def test_a_note_changes_neither_state_nor_reason_nor_age(gen, clock, ledger, stock):
    sale = gen.make()
    checked(ledger, stock, sale)
    before = ledger.find(so_number=sale.so_number)
    clock.advance(hours=1)
    ledger.annotate(sale.so_number, ALERTED, "resolver", fingerprint="abc")
    after = ledger.find(so_number=sale.so_number)
    assert (after.state, after.reason, after.updated_at) == (before.state, before.reason, before.updated_at)
    assert after.fingerprint == "abc"
    assert ledger.history(sale.so_number)[-1].is_note
