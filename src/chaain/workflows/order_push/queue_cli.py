"""The team queue as a command line: `make queue AS="Ops lead"`.

The demo's stock system and warehouse are in-memory mocks, so this is one
interactive session. It seeds a late morning's orders, runs the poll and
the resolver once, then takes commands. Every action is logged under the
name given with --as.

The notes reader (the AI step) is off here so the queue runs offline and
free; the holds come from the rules.
"""

from __future__ import annotations

import argparse
import cmd
import shlex
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from chaain.connectors.notifier.interface import Notifier
from chaain.connectors.notifier.mock import ConsoleNotifier
from chaain.connectors.stock_system.generator import CATALOGUE, SaleGenerator
from chaain.connectors.stock_system.mock import MockStockSystem
from chaain.connectors.warehouse.mock import Behaviour, MockWarehouse
from chaain.core.config import load_rules, load_settings
from chaain.core.decision import Decision
from chaain.core.ledger import Ledger
from chaain.core.ledger.sqlite import SqliteLedger
from chaain.core.models import Sale
from chaain.workflows.order_push.gate import GateRules, Reference, gate_for
from chaain.workflows.order_push.queue import (
    SECTIONS, Cutoffs, Item, QueueError, approve, build_queue, mark_received, mark_retry, reject,
)
from chaain.workflows.order_push.resolver import ResolverSettings, resolve
from chaain.workflows.order_push.run import RunSettings, run_once

UK = ZoneInfo("Europe/London")
DEMO_START = datetime(2026, 11, 4, 11, 40, tzinfo=UK)  # a Wednesday, 50 minutes to the cut-off


@dataclass
class Workspace:
    ledger: Ledger
    stock: MockStockSystem
    warehouse: MockWarehouse
    gate: Callable[[Sale], Decision]
    notifier: Notifier
    cutoffs: Cutoffs
    resolver: ResolverSettings
    run: RunSettings
    clock: Callable[[], datetime]


def demo_workspace(notifier: Notifier | None = None, start: datetime = DEMO_START) -> Workspace:
    """A late morning: some orders sent, some held, one refused, one that
    timed out, and one claimed 40 minutes ago with no outcome."""
    started = datetime.now(timezone.utc)
    now = [start - timedelta(minutes=40)]  # the clock moves with real time once seeded

    def clock() -> datetime:
        return now[0] if now[0] < start else start + (datetime.now(timezone.utc) - started)

    settings = load_settings()
    gen = SaleGenerator(seed=3, now=start)
    ledger = SqliteLedger(clock=clock)
    stock = MockStockSystem()
    wh = MockWarehouse(today=start.date(), script=[Behaviour.timeout(received=True), Behaviour.reject()])
    rules = GateRules.from_config(load_rules())
    newcomer = gen.make("new_customer")
    ref = Reference(
        warehouse_stock={code: 1000 for code, *_ in CATALOGUE},
        known_customers=frozenset(c.id for c in gen.customers),
    )
    gate = gate_for(ref, rules, wh.prepare)

    # 40 minutes ago: a run claimed this order, then died before sending.
    stuck = gen.make()
    stock.add(stuck)
    ledger.open(stuck.so_number, stuck.id, stuck.customer_id, "first seen in poll")
    ledger.claim(stuck.so_number, "claimed for sending")
    now[0] = start

    stock.add(gen.make(), gen.make(), gen.make(), gen.make("mixed_pick"), newcomer, gen.make("missing_town"))
    ws = Workspace(
        ledger, stock, wh, gate, notifier or ConsoleNotifier(), Cutoffs.from_config(settings),
        ResolverSettings.from_config(settings), RunSettings.from_config(settings["scheduler"]), clock,
    )
    run_once(clock(), stock, ledger, wh, gate, ws.run)
    resolve(clock(), ledger, wh, ws.notifier, ws.resolver)
    return ws


# ---------- rendering ----------

def render_queue(items: list[Item], now: datetime, cutoffs: Cutoffs) -> str:
    lines = [f"Team queue, {now.astimezone(cutoffs.tz):%a %d %b %H:%M}. {cutoffs.describe(now)}."]
    if not items:
        return lines[0] + "\n\nNothing needs a person."
    for section, title in SECTIONS.items():
        group = [i for i in items if i.section == section]
        if not group:
            continue
        lines += ["", f"{title} ({len(group)})"]
        for i in group:
            waited = _ago(now - i.since)
            lines.append(f"  {i.so_number:<10} {i.customer:<28} {i.state.value:<8} waiting {waited}")
            for r in i.reasons:
                lines.append(f"      - {r}")
            if i.actions:
                lines.append(f"      actions: {', '.join(i.actions)}")
    return "\n".join(lines)


def render_item(item: Item, history: list, tz: ZoneInfo) -> str:
    lines = [f"{item.so_number}  {item.customer}  ({item.state.value})", "", "Reasons:"]
    lines += [f"  {n}. {r}" for n, r in enumerate(item.reasons, 1)]
    if item.evidence:
        lines += ["", "Evidence:"] + [f"  {e}" for e in item.evidence]
    if item.question:
        lines += ["", item.question]
    lines += ["", "History:"]
    for e in history:
        arrow = "note" if e.is_note else f"{e.from_state.value if e.from_state else '-'} -> {e.to_state.value}"
        lines.append(f"  {e.at.astimezone(tz):%a %H:%M}  {e.actor:<12} {arrow:<22} {e.reason}")
    return "\n".join(lines)


def _ago(d: timedelta) -> str:
    minutes = max(0, int(d.total_seconds() // 60))
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


# ---------- the shell ----------

class QueueShell(cmd.Cmd):
    intro = 'Type "list" to see the queue, "help" for commands, "quit" to leave.'

    def __init__(self, ws: Workspace, actor: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.ws, self.actor = ws, actor
        self.prompt = f"queue ({actor})> "

    def _items(self) -> list[Item]:
        return build_queue(self.ws.clock(), self.ws.ledger, self.ws.stock, self.ws.resolver.stale_pending_minutes,
                           self.ws.cutoffs.tz)

    def _do(self, action: Callable[[], str]) -> None:
        try:
            print(action())
        except QueueError as e:
            print(f"Not done: {e}")

    def do_list(self, arg: str) -> None:
        """list: everything waiting for a person, most urgent first."""
        print(render_queue(self._items(), self.ws.clock(), self.ws.cutoffs))

    def do_show(self, arg: str) -> None:
        """show SO-NUMBER: reasons, evidence and full history for one order."""
        so = arg.strip().upper()
        item = next((i for i in self._items() if i.so_number == so), None)
        if item is None:
            print(f"{so or '(no SO number)'} isn't in the queue.")
            return
        print(render_item(item, self.ws.ledger.history(so), self.ws.cutoffs.tz))

    def do_approve(self, arg: str) -> None:
        """approve SO-NUMBER: send it now (if it changed since it was held, you'll see it again first)."""
        ws = self.ws
        self._do(lambda: approve(arg.strip(), self.actor, ledger=ws.ledger, stock=ws.stock,
                                 warehouse=ws.warehouse, gate=ws.gate))

    def do_reject(self, arg: str) -> None:
        """reject SO-NUMBER WHY: never send it. Final. Not sure yet? Leave it held."""
        so, _, why = arg.strip().partition(" ")
        self._do(lambda: reject(so, self.actor, why, ledger=self.ws.ledger))

    def do_received(self, arg: str) -> None:
        """received SO-NUMBER [NOTE]: you found it at the warehouse. It won't be sent again."""
        so, _, note = arg.strip().partition(" ")
        self._do(lambda: mark_received(so, self.actor, ledger=self.ws.ledger, note=note))

    def do_retry(self, arg: str) -> None:
        """retry SO-NUMBER [NOTE]: it isn't at the warehouse (or was fixed at source). The next poll sends it."""
        so, _, note = arg.strip().partition(" ")
        self._do(lambda: mark_retry(so, self.actor, ledger=self.ws.ledger, note=note))

    def do_poll(self, arg: str) -> None:
        """poll: run the stock-system poll now, as the scheduler would."""
        report = run_once(self.ws.clock(), self.ws.stock, self.ws.ledger, self.ws.warehouse, self.ws.gate, self.ws.run)
        acted = [f"  {so}: {action}. {why}" for so, action, why in report.actions if action != "reported"]
        print("\n".join(acted) if acted else "Nothing to do.")

    def do_resolve(self, arg: str) -> None:
        """resolve: run the resolver now, as its 15-minute schedule would."""
        r = resolve(self.ws.clock(), self.ws.ledger, self.ws.warehouse, self.ws.notifier, self.ws.resolver)
        print(f"Moved to CHECK: {len(r.made_check)}. Alerted: {len(r.alerted)}."
              + (f" Alert failed: {r.alert_failed}" if r.alert_failed else ""))

    def do_quit(self, arg: str) -> bool:
        """quit: leave. The demo's state isn't kept."""
        return True

    do_EOF = do_quit

    def default(self, line: str) -> None:
        print(f'Unknown command "{shlex.split(line)[0] if line.strip() else line}". Type "help".')

    def emptyline(self) -> None:
        pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="queue", description="The team queue (demo, offline).")
    parser.add_argument("--as", dest="actor", required=True, help='who you are, e.g. "Ops lead"')
    args = parser.parse_args(argv)
    if not args.actor.strip():
        parser.error('--as needs a name, e.g. --as "Ops lead"')
    QueueShell(demo_workspace(), args.actor.strip()).cmdloop()


if __name__ == "__main__":
    main()
