#!/usr/bin/env python3
"""Provider Registry — Strategy + Registry pattern for LLM provider routing.

Eliminates all ``if codex / else ollama`` conditional branching by resolving
the correct LLM backend at call time via O(1) dictionary lookup.

Design Principles (FAANG-grade):
    - Protocol-based interface (structural typing, no inheritance coupling)
    - Dependency Inversion: callers depend on LLMProvider, never on concrete classes
    - Single Responsibility: registry resolves providers; providers handle LLM I/O
    - Open/Closed: new providers are added via ``register()`` without modifying existing code

Usage::

    registry = get_registry()
    provider = registry.resolve(task)
    response = await provider.chat(client, messages, model="qwen3.5:35b-a3b")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from utils.logger import get_logger

logger = get_logger("providers.registry")

# ---------------------------------------------------------------------------
# Unified Response Envelope
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """Unified response from any LLM provider.

    Normalizes the output across Gateway (Ollama/Frontier) and Codex backends
    into a single envelope that all agent roles consume identically.
    """

    content: str
    """Raw text content from the LLM response."""

    usage: dict[str, Any] = field(default_factory=dict)
    """Token usage metrics (prompt_tokens, completion_tokens, etc.)."""

    telemetry: dict[str, Any] = field(default_factory=dict)
    """Execution telemetry (node_id, node_host, model, latency)."""

    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """Tool calls returned by the model (function-calling mode)."""

    thoughts: str = ""
    """Model reasoning/thinking content if available."""

    raw: dict[str, Any] = field(default_factory=dict)
    """Original unmodified response dict for provider-specific access."""


# ---------------------------------------------------------------------------
# Provider Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """Contract that all LLM providers must satisfy.

    Uses ``typing.Protocol`` (structural typing) rather than ABC inheritance
    so that providers do not need to explicitly subclass. Any class with
    matching ``chat`` and ``chat_with_tools`` signatures satisfies the contract.
    """

    @property
    def name(self) -> str:
        """Human-readable provider name for logging and telemetry."""
        ...

    async def chat(
        self,
        client: Any,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        timeout_seconds: int = 300,
        json_schema: Optional[dict[str, Any]] = None,
        agent_role: str = "",
        task_id: Optional[str] = None,
        task: Optional[dict[str, Any]] = None,
    ) -> LLMResponse:
        """Send a chat completion request and return a unified response."""
        ...

    async def chat_with_tools(
        self,
        client: Any,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        agent_role: str = "",
        task_id: Optional[str] = None,
        task: Optional[dict[str, Any]] = None,
    ) -> LLMResponse:
        """Send a chat completion request with tool definitions."""
        ...


# ---------------------------------------------------------------------------
# Provider Registry
# ---------------------------------------------------------------------------


class ProviderRegistry:
    """Singleton registry that resolves the correct LLM provider per task.

    Providers are registered at boot time. Resolution is O(1) — a single
    dictionary lookup based on the task's credential/provider configuration.
    """

    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}
        self._default_name: Optional[str] = None

    def register(self, name: str, provider: LLMProvider, *, default: bool = False) -> None:
        """Register a provider under a unique name.

        Args:
            name: Unique identifier (e.g., ``"gateway"``, ``"codex"``).
            provider: An object satisfying the ``LLMProvider`` protocol.
            default: If True, this provider becomes the fallback for unresolved tasks.
        """
        if not isinstance(provider, LLMProvider):
            raise TypeError(
                f"Provider '{name}' does not satisfy the LLMProvider protocol. "
                f"Missing methods: chat, chat_with_tools, name property."
            )
        self._providers[name] = provider
        if default or self._default_name is None:
            self._default_name = name
        logger.info("Registered LLM provider: %s (default=%s)", name, default)

    def resolve(self, task: Optional[dict[str, Any]] = None) -> LLMProvider:
        """Resolve the correct provider for a task.

        Resolution order:
            1. Task specifies ``preferred_llm_credential_id`` matching a registered
               Codex/OpenAI credential → ``codex`` provider (if registered).
            2. Task specifies ``preferred_llm_provider`` matching a registered name.
            3. Fall back to the default provider (``gateway``).

        Args:
            task: The task dict containing provider/credential preferences.

        Returns:
            The resolved ``LLMProvider`` instance.

        Raises:
            RuntimeError: If no providers are registered.
        """
        if not self._providers:
            raise RuntimeError(
                "No LLM providers registered. Ensure providers are initialized "
                "during worker boot (see providers.bootstrap)."
            )

        if task:
            # 1. Check for explicit Codex credential binding
            cred_id = str(task.get("preferred_llm_credential_id") or "").strip()
            if cred_id and "codex" in self._providers:
                # Delegate to the Codex provider's own eligibility check
                codex = self._providers["codex"]
                if hasattr(codex, "accepts_task") and codex.accepts_task(task):  # type: ignore[attr-defined]
                    logger.info(
                        "Resolved provider 'codex' for task %s (credential_id=%s)",
                        task.get("id", "?"),
                        cred_id,
                    )
                    return codex

            # 2. Check for explicit provider name preference
            prov_name = str(task.get("preferred_llm_provider") or "").strip().lower()
            if prov_name and prov_name in self._providers:
                logger.info(
                    "Resolved provider '%s' for task %s (explicit preference)",
                    prov_name,
                    task.get("id", "?"),
                )
                return self._providers[prov_name]

        # 3. Default provider (gateway)
        default = self._providers.get(self._default_name or "")
        if default is None:
            # Fallback to first registered provider
            default = next(iter(self._providers.values()))
        return default

    @property
    def registered_providers(self) -> list[str]:
        """List of all registered provider names."""
        return list(self._providers.keys())

    def get(self, name: str) -> Optional[LLMProvider]:
        """Get a provider by exact name, or None if not registered."""
        return self._providers.get(name)


# ---------------------------------------------------------------------------
# Singleton + Bootstrap
# ---------------------------------------------------------------------------

_registry: Optional[ProviderRegistry] = None


def get_registry() -> ProviderRegistry:
    """Return the singleton ProviderRegistry, bootstrapping on first call.

    Bootstrap registers the GatewayProvider (always available) and the
    CodexProvider (only when OpenAI credentials + Codex binary are present).
    """
    global _registry
    if _registry is not None:
        return _registry

    _registry = ProviderRegistry()

    # ── Register GatewayProvider (always available, default) ──────────────
    from providers.gateway import GatewayProvider

    _registry.register("gateway", GatewayProvider(), default=True)

    # ── Register CodexProvider (opt-in only) ─────────────────────────────
    try:
        from providers.codex import CodexProvider

        codex = CodexProvider()
        if codex.is_available():
            _registry.register("codex", codex)
            logger.info("CodexProvider registered — OpenAI Codex integration available")
        else:
            logger.info("CodexProvider not registered — credentials or binary not present")
    except Exception as exc:
        logger.warning("CodexProvider registration skipped: %s", exc)

    logger.info(
        "Provider registry initialized: providers=%s, default=%s",
        _registry.registered_providers,
        _registry._default_name,
    )
    return _registry


def reset_registry() -> None:
    """Reset the singleton registry (for testing only)."""
    global _registry
    _registry = None
