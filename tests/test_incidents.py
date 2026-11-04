"""One test per past incident or near-miss, numbered as in the brief.
Each recreates the conditions and proves the safe outcome."""

import threading
import time
from dataclasses import replace
from datetime import date, timedelta

import pytest

from chaain.connectors.stock_system.generator import DEFAULT_NOW, SaleGenerator
from chaain.connectors.stock_system.mock import MockStockSystem
from chaain.connectors.warehouse.mock import Behaviour, MockWarehouse
from chaain.core.decision import Decision, Route
from chaain.core.ledger import State
from chaain.core.ledger.sqlite import SqliteLedger
from chaain.core.models import SaleStatus
from chaain.workflows.order_push.run import RunSettings, run_once

NOW = DEFAULT_NOW


def send_everything(sale):
    return Decision(Route.AUTO, ())


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def gen():
    return SaleGenerator(seed=11)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ledger(clock):
    return SqliteLedger(clock=clock)


def warehouse(*script, **kwargs):
    return MockWarehouse(script=script, today=date(2026, 11, 4), **kwargs)


def mark_sent(ledger, sale):
    ledger.open(sale.so_number, sale.id, sale.customer_id, "first seen")
    ledger.claim(sale.so_number, "AUTO")
    ledger.move(sale.so_number, State.SENT, "accepted")


# ---------- the incidents ----------

def test_1_cap_applies_after_dedup(gen, ledger):
    """The live cap was applied before dedup. Here: 17 orders, 7 already sent, a cap of 10.
    7 of the 17 sat invisible. Now dedup runs first, so all 10 new ones go."""
    sales = [gen.make() for _ in range(17)]
    for sale in sales[:7]:
        mark_sent(ledger, sale)
    wh = warehouse()

    report = run_once(NOW, MockStockSystem(sales), ledger, wh, send_everything)

    assert sorted(wh.attempts) == sorted(s.so_number for s in sales[7:])
    assert report.count("sent") == 10 and report.deferred == []


def test_2_accepted_but_ledger_write_failed_is_not_resent(gen, ledger, monkeypatch):
    """October: the warehouse accepted, the log write hit a 429, the next run
    resent it, and the warehouse received it twice."""
    sale = gen.make()
    stock, wh = MockStockSystem([sale]), warehouse()
    real_move = ledger.move

    def write_fails_on_sent(so, to, reason, *args, **kwargs):
        if to is State.SENT:
            raise RuntimeError("429 Too Many Requests")
        return real_move(so, to, reason, *args, **kwargs)

    monkeypatch.setattr(ledger, "move", write_fails_on_sent)
    run_once(NOW, stock, ledger, wh, send_everything)
    run_once(NOW + timedelta(minutes=15), stock, ledger, wh, send_everything)

    assert ledger.find(so_number=sale.so_number).state is State.CHECK
    assert wh.received[sale.so_number] == 1 and wh.attempts == [sale.so_number]


def test_3_timeout_goes_to_check_and_is_never_resent(gen, ledger):
    """Auto-retry after an ambiguous response was removed after it risked
    duplicates. A timeout now waits for a person."""
    sale = gen.make()
    stock, wh = MockStockSystem([sale]), warehouse(Behaviour.timeout())

    for minutes in (0, 15, 30, 45):
        run_once(NOW + timedelta(minutes=minutes), stock, ledger, wh, send_everything)

    assert ledger.find(so_number=sale.so_number).state is State.CHECK
    assert wh.attempts == [sale.so_number] and wh.received[sale.so_number] == 1


def test_7_lookback_across_the_manual_to_automatic_boundary(gen, ledger):
    """Near-miss: the first run's lookback reached orders already keyed into
    the warehouse by hand. Cutover imports them as manual SENT rows; anything
    invoiced more than 7 days ago is ignored even if a sync touches it."""
    keyed_by_hand = [gen.make() for _ in range(3)]
    for sale in keyed_by_hand:
        ledger.record_manual(sale.so_number, sale.id, actor="cutover import", reason="keyed by hand before go-live")
    resurfaced = gen.make("old_invoice")  # no row: invoiced 10 days ago, touched by a payment sync
    new = gen.make()
    wh = warehouse()

    report = run_once(NOW, MockStockSystem(keyed_by_hand + [resurfaced, new]), ledger, wh, send_everything)

    assert wh.attempts == [new.so_number]
    assert (resurfaced.so_number, "ignored", "invoiced over 7 days ago") in report.actions


def test_8_truncated_guid_on_a_manual_entry_still_blocks(gen, ledger):
    """Near-miss: a hand-entered log row had its GUID cut short on paste and
    didn't block a resend. The SO number alone now blocks."""
    sale = gen.make()
    ledger.record_manual(sale.so_number, sale.id[:13], actor="Ops lead", reason="keyed by hand")
    wh = warehouse()

    run_once(NOW, MockStockSystem([sale]), ledger, wh, send_everything)

    assert wh.attempts == []


def test_13_two_runs_at_once_send_each_order_at_most_once(gen, tmp_path):
    """Two overlapping runs, each with its own database connection, racing
    over the same orders. The claim is the single point only one can win."""
    sales = [gen.make() for _ in range(10)]
    stock = MockStockSystem(sales)
    lock = threading.Lock()

    class SlowWarehouse(MockWarehouse):
        def send(self, order):
            time.sleep(0.005)  # widen the window for a race
            with lock:
                return super().send(order)

    wh = SlowWarehouse(today=date(2026, 11, 4))
    path = tmp_path / "ledger.db"
    ledgers = [SqliteLedger(path, clock=Clock()) for _ in range(2)]
    start = threading.Barrier(2)

    def run(ledger):
        start.wait()
        run_once(NOW, stock, ledger, wh, send_everything)

    threads = [threading.Thread(target=run, args=(l,)) for l in ledgers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(wh.received[s.so_number] == 1 for s in sales)
    assert all(ledgers[0].find(so_number=s.so_number).state is State.SENT for s in sales)


# ---------- the rules around them ----------

def test_not_invoiced_gets_no_row_and_is_sent_once_invoiced(gen, ledger, clock):
    sale = gen.make("not_invoiced")
    stock, wh = MockStockSystem([sale]), warehouse()

    run_once(NOW, stock, ledger, wh, send_everything)
    assert ledger.find(so_number=sale.so_number) is None

    stock.update(sale.id, NOW + timedelta(minutes=10), status=SaleStatus.INVOICED, invoice_date=NOW)
    run_once(NOW + timedelta(minutes=15), stock, ledger, wh, send_everything)
    assert wh.attempts == [sale.so_number]


def test_location_skip_is_rechecked_when_the_sale_changes_but_final_skip_never(gen, ledger, clock):
    shopify = gen.make("shopify")
    novision = gen.make("novision_note")
    stock, wh = MockStockSystem([shopify, novision]), warehouse()

    def gate(sale):
        if "#NOVISION" in sale.note:
            return Decision(Route.SKIP, ("#NOVISION in note",), final=True)
        if sale.pick_locations == {"Site-B"}:
            return Decision(Route.SKIP, ("picked at Site-B",))
        return Decision(Route.AUTO, ())

    run_once(NOW, stock, ledger, wh, gate)
    # Someone switches the Shopify pick to the warehouse and removes the marker.
    clock.now = NOW + timedelta(minutes=5)
    later = NOW + timedelta(minutes=30)
    stock.update(shopify.id, later, lines=tuple(replace(l, pick_location="Warehouse-3PL") for l in shopify.lines))
    stock.update(novision.id, later, note="re-bill")
    run_once(later + timedelta(minutes=5), stock, ledger, wh, gate)

    assert wh.attempts == [shopify.so_number]
    assert ledger.find(so_number=novision.so_number).final


def test_a_change_after_sending_is_flagged_not_resent(gen, ledger):
    sale = gen.make()
    stock, wh = MockStockSystem([sale]), warehouse()
    run_once(NOW, stock, ledger, wh, send_everything)

    stock.update(sale.id, NOW + timedelta(minutes=10), status=SaleStatus.CREDITED)
    report = run_once(NOW + timedelta(minutes=15), stock, ledger, wh, send_everything)

    assert report.changed_after_send == [sale.so_number]
    assert wh.attempts == [sale.so_number]


def test_auth_failure_stops_the_run_and_defers_the_rest(gen, ledger):
    sales = [gen.make() for _ in range(4)]
    wh = warehouse(Behaviour.auth_fail())

    report = run_once(NOW, MockStockSystem(sales), ledger, wh, send_everything)

    assert len(wh.attempts) == 1 and len(report.deferred) == 3 and report.stopped


def test_cap_defers_the_newest_to_the_next_run(gen, ledger):
    sales = [gen.make() for _ in range(12)]
    stock, wh = MockStockSystem(sales), warehouse()

    first = run_once(NOW, stock, ledger, wh, send_everything, RunSettings(per_run_cap=10))
    second = run_once(NOW + timedelta(minutes=5), stock, ledger, wh, send_everything)

    assert len(first.deferred) == 2 and second.count("sent") == 2
    assert sorted(wh.attempts) == sorted(s.so_number for s in sales)


def test_a_sale_the_warehouse_would_refuse_is_held_for_a_person(gen, ledger):
    sale = gen.make("no_phone")
    wh = warehouse()

    run_once(NOW, MockStockSystem([sale]), ledger, wh, send_everything)

    entry = ledger.find(so_number=sale.so_number)
    assert entry.state is State.HELD and "phone" in entry.reason and wh.attempts == []
