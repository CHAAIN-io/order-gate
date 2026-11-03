"""A provider for tests and offline demos: scripted answers, no network."""

from __future__ import annotations

from typing import Callable

from chaain.llm.interface import LLMError, LLMProvider


class FakeProvider(LLMProvider):
    name = "fake"
    model = "fake-model"

    def __init__(self, answer: Callable[[str], dict] | None = None, script: list | tuple = ()) -> None:
        """`script` items are used first, in order: a dict is returned, an
        exception is raised. Then `answer(user_text)` decides."""
        self.script = list(script)
        self.answer = answer
        self.calls: list[tuple[str, str]] = []  # (system, user) per call

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        self.calls.append((system, user))
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if self.answer is None:
            raise LLMError("fake provider has no answer")
        return self.answer(user)
