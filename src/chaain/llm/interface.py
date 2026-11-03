"""One way to ask a model for structured JSON, whoever provides the model.

Anthropic directly, Claude through a client's own cloud account, or a fake in
tests all implement this. Callers never import a provider's SDK.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMError(Exception):
    """Any failure to get a usable answer: timeout, outage, refusal, malformed output.
    Callers treat them all the same way, so there's one type."""


class LLMProvider(ABC):
    name: str = "unknown"
    model: str = "unknown"

    @abstractmethod
    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        """One attempt. Returns JSON matching `schema`, or raises LLMError.
        No retries here: the caller decides, because only it knows whether
        asking again is safe."""
