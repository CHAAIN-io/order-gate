"""The queue's command line, driven the way a person would use it."""

import pytest

from chaain.connectors.notifier.mock import MemoryNotifier
from chaain.core.ledger import State
from chaain.workflows.order_push.queue_cli import QueueShell, demo_workspace, main


@pytest.fixture
def shell():
    return QueueShell(demo_workspace(MemoryNotifier()), "Ops lead")


def held_so(shell, reason_part):
    return next(i.so_number for i in shell._items() if i.state is State.HELD and reason_part in i.reasons[0])


def test_the_demo_day_has_every_kind_of_item_and_each_was_alerted_once(shell):
    items = shell._items()
    assert {i.state for i in items} == {State.CHECK, State.HELD, State.REJECTED}
    alerted = [line.split()[1] for _, body in shell.ws.notifier.sent for line in body.splitlines()
               if line.startswith("- ")]
    assert sorted(alerted) == sorted(i.so_number for i in items)


def test_list_shows_the_cut_off_and_every_section(shell, capsys):
    shell.onecmd("list")
    out = capsys.readouterr().out
    assert "past today's 13:00 warehouse cut-off" in out  # the demo day ends at 13:30
    for title in ("May or may not be at the warehouse", "Held for a decision", "Refused by the warehouse"):
        assert title in out


def test_approve_from_the_shell_sends_and_logs_who(shell, capsys):
    so = held_so(shell, "First order")
    shell.onecmd(f"approve {so}")
    assert "SENT" in capsys.readouterr().out
    assert any(e.actor == "Ops lead" for e in shell.ws.ledger.history(so))


def test_a_refused_action_says_why_and_changes_nothing(shell, capsys):
    so = held_so(shell, "delivery town")
    shell.onecmd(f"approve {so}")  # the warehouse would refuse it: fix at source first
    assert "Not done" in capsys.readouterr().out
    assert shell.ws.ledger.find(so_number=so).state is State.HELD


def test_reject_without_a_reason_is_refused(shell, capsys):
    so = held_so(shell, "Mixed pick")
    shell.onecmd(f"reject {so}")
    assert "say why" in capsys.readouterr().out


def test_show_prints_reasons_evidence_and_history(shell, capsys):
    so = next(i.so_number for i in shell._items() if i.state is State.CHECK)
    shell.onecmd(f"show {so}")
    out = capsys.readouterr().out
    assert "Reasons:" in out and "History:" in out and f"doc ref {so}" in out


def test_a_name_is_required():
    with pytest.raises(SystemExit):
        main(["--as", "  "])
