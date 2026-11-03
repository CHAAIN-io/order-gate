"""Notifiers for tests and the demo: no email leaves the machine."""

from __future__ import annotations

from chaain.connectors.notifier.interface import Notifier, NotifyError


class MemoryNotifier(Notifier):
    """Keeps every message, and can be told to fail."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def notify(self, subject: str, body: str) -> None:
        if self.fail:
            raise NotifyError("mail server unavailable")
        self.sent.append((subject, body))


class ConsoleNotifier(Notifier):
    def notify(self, subject: str, body: str) -> None:
        print(f"\n[ALERT] {subject}\n{body}\n")
