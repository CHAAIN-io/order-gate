"""A provider that answers from a saved run: real model answers, replayed offline.

For demos: the answers were given by the real model during an eval run, so
the AI step shows what it really does, without a key, a network or a
different answer on the day. Text it has no recorded answer for fails like
any other unusable answer.
"""

from __future__ import annotations

from chaain.llm.interface import LLMError, LLMProvider


class ReplayProvider(LLMProvider):
    name = "replay"

    def __init__(self, answers: dict[str, dict], model: str) -> None:
        """`answers` maps the exact user text to the answer the model gave;
        `model` is the model that gave them, for the audit trail."""
        self.answers = answers
        self.model = model

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        try:
            return self.answers[user]
        except KeyError:
            raise LLMError("no recorded answer for this text") from None
