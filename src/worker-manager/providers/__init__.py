#!/usr/bin/env python3
"""Flume LLM Provider Abstraction Layer.

Exposes a singleton ProviderRegistry that resolves the correct LLM backend
at call time based on task configuration. This replaces all
``if _task_uses_codex_app_server ... else _call_ollama`` branches with
a clean Strategy + Registry pattern.

Usage::

    from providers import get_registry
    registry = get_registry()
    provider = registry.resolve(task)
    response = await provider.chat(client, messages, model=model)
"""

from providers.registry import (  # noqa: F401
    LLMProvider,
    LLMResponse,
    ProviderRegistry,
    get_registry,
)

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "ProviderRegistry",
    "get_registry",
]
