"""Polling follows the team's day: every 15 minutes 08:00-20:00, every 5
minutes in the run-up to the 12:30 cut-off, nothing overnight."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from chaain.core.config import load_settings
from chaain.workflows.order_push.scheduler import Schedule, run_forever

UK = ZoneInfo("Europe/London")
SCHEDULE = Schedule.from_config(load_settings("demo")["scheduler"])


def at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, day, h, m, tzinfo=UK)


@pytest.mark.parametrize("after, expected", [
    (at(4, "07:59"), at(4, "08:00")),
    (at(4, "08:00"), at(4, "08:15")),
    (at(4, "10:46"), at(4, "11:00")),   # 10:45 is past, 11:00 starts the busy window
    (at(4, "11:00"), at(4, "11:05")),
    (at(4, "12:27"), at(4, "12:30")),
    (at(4, "13:25"), at(4, "13:30")),
    (at(4, "13:30"), at(4, "13:45")),   # back to every 15 minutes
    (at(4, "19:50"), at(4, "20:00")),
    (at(4, "20:00"), at(5, "08:00")),   # nothing overnight
])
def test_next_run(after, expected):
    assert SCHEDULE.next_run(after) == expected


def test_runs_per_day():
    runs, t = [], at(4, "00:00")
    while (t := SCHEDULE.next_run(t)) < at(5, "00:00"):
        runs.append(t.astimezone(UK).strftime("%H:%M"))
    busy = [r for r in runs if "11:00" <= r <= "13:30"]
    assert len(busy) == 31 and busy[:3] == ["11:00", "11:05", "11:10"]
    assert runs[0] == "08:00" and runs[-1] == "20:00"
    assert len(runs) == 31 + 12 + 26  # busy window, 08:00-10:45, 13:45-20:00


def test_clock_change_keeps_local_times():
    # Clocks went back on Sunday 25 October 2026: 08:00 local is 08:00 UTC after, 07:00 UTC before.
    before = SCHEDULE.next_run(datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc))
    after = SCHEDULE.next_run(datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc))
    assert before.astimezone(UK).strftime("%H:%M") == after.astimezone(UK).strftime("%H:%M") == "08:00"
    assert before.hour == 7 and after.hour == 8


def test_run_forever_sleeps_until_each_due_time():
    clock = {"now": at(4, "07:55").astimezone(timezone.utc)}
    slept, ran = [], []

    def sleep(seconds):
        slept.append(seconds)
        clock["now"] += timedelta(seconds=seconds)

    run_forever(SCHEDULE, ran.append, now=lambda: clock["now"], sleep=sleep, max_runs=3)
    assert [r.astimezone(UK).strftime("%H:%M") for r in ran] == ["08:00", "08:15", "08:30"]
    assert slept == [300, 900, 900]
