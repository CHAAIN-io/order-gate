"""How the system tells the team something needs them.

The live integration emails. The demo keeps messages in memory or prints
them; an email or Slack connector would implement the same method.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class NotifyError(Exception):
    """The message didn't go. The caller tries again on its next run."""


class Notifier(ABC):
    @abstractmethod
    def notify(self, subject: str, body: str) -> None:
        """Send one message, or raise NotifyError."""
