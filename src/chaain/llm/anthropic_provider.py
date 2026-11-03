"""Claude through the Anthropic API.

The key is never handled here: the SDK reads ANTHROPIC_API_KEY (or another
configured credential source) from the environment itself.

Structured outputs constrain the answer to our JSON schema. On models that
support it, a server-side fallback re-runs a request the model's safety
classifiers decline, instead of returning a refusal.
"""

from __future__ import annotations

import json

import anthropic

from chaain.llm.interface import LLMError, LLMProvider

# Models that accept `fallbacks: "default"` on the Claude API.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self, model: str, effort: str = "low", max_tokens: int = 4000,
        timeout_seconds: float = 30, refusal_fallback: bool = True, client=None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.fallback = refusal_fallback and model in FALLBACK_MODELS
        self.timeout_seconds = timeout_seconds
        self._client = client

    @property
    def client(self):
        """Created on first use, so nothing needs credentials until a note is read."""
        if self._client is None:
            # max_retries=0: retrying is the caller's decision (see interface).
            self._client = anthropic.Anthropic(timeout=self.timeout_seconds, max_retries=0)
        return self._client

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        request = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            if self.fallback:
                response = self.client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **request
                )
            else:
                response = self.client.messages.create(**request)
        except anthropic.APITimeoutError as e:
            raise LLMError("timed out") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("couldn't connect") from e
        except anthropic.AuthenticationError as e:
            raise LLMError("credentials rejected") from e
        except anthropic.RateLimitError as e:
            raise LLMError("rate limited") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"API error {e.status_code}") from e
        except anthropic.AnthropicError as e:
            raise LLMError(f"client error: {type(e).__name__}") from e
        except TypeError as e:
            # The SDK reports missing credentials (e.g. an empty ANTHROPIC_API_KEY)
            # as a TypeError. Any other TypeError is our bug and should surface.
            if "authentication method" not in str(e):
                raise
            raise LLMError("no credentials configured") from e

        if response.stop_reason == "refusal":
            raise LLMError("the model declined to read this note")
        if response.stop_reason == "max_tokens":
            raise LLMError("answer cut off at max_tokens")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("no text in the answer")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError("answer wasn't valid JSON") from e
