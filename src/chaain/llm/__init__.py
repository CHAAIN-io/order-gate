"""LLM adapter: provider interface plus implementations (Anthropic first).

Provider, model and settings come from the client profile and environment,
so a client can use their own account later without code changes.
"""

from __future__ import annotations

import os

from chaain.llm.interface import LLMError, LLMProvider

__all__ = ["LLMError", "LLMProvider", "provider_from_config"]


def provider_from_config(llm: dict, env: dict | None = None) -> LLMProvider:
    """Build the configured provider. The model comes from the environment
    variable named in config if set, else the config default."""
    env = os.environ if env is None else env
    model = env.get(llm.get("model_env", "ANTHROPIC_MODEL")) or llm["default_model"]
    if llm["provider"] == "anthropic":
        from chaain.llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider(
            model=model, effort=llm.get("effort", "low"), max_tokens=llm.get("max_tokens", 4000),
            timeout_seconds=llm.get("timeout_seconds", 30),
            refusal_fallback=llm.get("refusal_fallback", True),
        )
    raise ValueError(f"unknown LLM provider {llm['provider']!r}")
