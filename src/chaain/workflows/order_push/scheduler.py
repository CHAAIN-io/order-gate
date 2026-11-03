"""When to poll: windows in local time, each with its own interval.

Runs land on a clock grid counted from each window's start (08:00, 08:15...
and 11:00, 11:05...), so the times are predictable for the team. Times are
local, so a clock change moves runs with the working day, not with UTC.
"""

from __future__ import annotations

import time as systime
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class Window:
    start: time
    end: time  # included
    every_minutes: int

    def due(self, local: datetime) -> bool:
        """Whether a run is due at this local minute under this window."""
        t = local.time().replace(second=0, microsecond=0)
        if not (self.start <= t <= self.end):
            return False
        since_start = (t.hour * 60 + t.minute) - (self.start.hour * 60 + self.start.minute)
        return since_start % self.every_minutes == 0

    def covers(self, local: datetime) -> bool:
        return self.start <= local.time().replace(second=0, microsecond=0) <= self.end


@dataclass(frozen=True)
class Schedule:
    windows: tuple[Window, ...]
    tz: ZoneInfo

    @classmethod
    def from_config(cls, scheduler: dict) -> "Schedule":
        return cls(
            windows=tuple(
                Window(time.fromisoformat(w["start"]), time.fromisoformat(w["end"]), w["every_minutes"])
                for w in scheduler["windows"]
            ),
            tz=ZoneInfo(scheduler["timezone"]),
        )

    def is_due(self, moment: datetime) -> bool:
        """The first window covering this time decides, so overlaps are unambiguous."""
        local = moment.astimezone(self.tz)
        for window in self.windows:
            if window.covers(local):
                return window.due(local)
        return False

    def next_run(self, after: datetime) -> datetime:
        """The first due minute strictly after `after`, in UTC."""
        t = after.astimezone(timezone.utc).replace(second=0, microsecond=0) + MINUTE
        for _ in range(3 * 24 * 60):  # windows recur daily; three days is plenty
            if self.is_due(t):
                return t
            t += MINUTE
        raise ValueError("no polling window in the next three days")


def run_forever(
    schedule: Schedule,
    run: Callable[[datetime], object],
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] | None = None,
    max_runs: int | None = None,
) -> None:
    """Sleep until each due time, then run. `max_runs` and the injectable
    clock exist for tests and the demo."""
    sleep = sleep or systime.sleep
    runs = 0
    while max_runs is None or runs < max_runs:
        due = schedule.next_run(now())
        wait = (due - now()).total_seconds()
        if wait > 0:
            sleep(wait)
        run(due)
        runs += 1
