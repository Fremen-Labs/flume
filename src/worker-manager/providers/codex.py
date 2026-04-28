#!/usr/bin/env python3
"""CodexProvider — Opt-in LLM provider for OpenAI Codex app-server.

This provider is ONLY registered when ALL of the following are true:
    1. The user has configured OpenAI credentials in OpenBao
    2. The user has explicitly selected Codex integration
    3. The ``codex`` binary (or ``npx @openai/codex``) is available on PATH

If any condition is false, this provider is never registered and the
``ProviderRegistry`` will never resolve to it.

Replaces the legacy ``_run_codex_json_task`` and ``_task_uses_codex_app_server``
functions from ``agent_runner.py``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

from providers.registry import LLMResponse
from utils.logger import get_logger

logger = get_logger("providers.codex")

# Resolve the project root for importing shared modules
_HERE = Path(__file__).resolve().parent.parent
_BASE = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


class CodexProvider:
    """LLM provider that routes through the OpenAI Codex app-server.

    Satisfies the ``LLMProvider`` protocol via structural typing.
    Only activated when the user has explicitly configured OpenAI
    credentials AND selected Codex integration.
    """

    @property
    def name(self) -> str:
        return "codex"

    def is_available(self) -> bool:
        """Check if Codex integration is available (credentials + binary)."""
        try:
            import codex_app_server_bridge as codex_bridge

            return codex_bridge.codex_auth_present() and codex_bridge.codex_available()
        except Exception as exc:
            logger.warning("Codex availability check failed: %s", exc)
            return False

    def accepts_task(self, task: dict[str, Any]) -> bool:
        """Determine if this provider should handle a specific task.

        Returns True only when the task's credential configuration
        explicitly targets the OpenAI Codex integration.
        """
        try:
            import llm_credentials_store as lcs
            import codex_app_server_bridge as codex_bridge

            cred_id = str(task.get("preferred_llm_credential_id") or "").strip()

            # Explicit Codex OAuth credential binding
            if cred_id == lcs.OPENAI_OAUTH_CREDENTIAL_ID:
                return codex_bridge.codex_auth_present() and codex_bridge.codex_available()

            # Non-OpenAI credential — not for Codex
            if cred_id and cred_id not in ("", lcs.SETTINGS_DEFAULT_CREDENTIAL_ID):
                return False

            # Check for OpenAI provider with OAuth state (no direct API key)
            provider = (task.get("preferred_llm_provider") or "").strip().lower()
            if provider and provider != "openai":
                return False

            api_key = (os.environ.get("LLM_API_KEY") or "").strip()
            has_oauth = bool(
                (os.environ.get("OPENAI_OAUTH_STATE_FILE") or "").strip()
                or (os.environ.get("OPENAI_OAUTH_STATE_JSON") or "").strip()
            )
            if provider == "openai" and has_oauth and not (
                api_key.startswith("sk-") or api_key.startswith("sk_")
            ):
                return codex_bridge.codex_auth_present() and codex_bridge.codex_available()

            return False
        except Exception as exc:
            logger.warning("Codex task acceptance check failed: %s", exc)
            return False

    # ── Chat (structured JSON response) ──────────────────────────────────

    async def chat(
        self,
        client: httpx.AsyncClient,
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
        """Send a structured JSON task to the Codex app-server.

        The Codex provider always enforces JSON schema output via the
        ``--output-schema`` flag, ensuring deterministic, parseable responses.
        """
        import codex_app_server_bridge as codex_bridge

        # Build a single prompt from messages
        prompt_parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "user").upper()
            content = msg.get("content", "")
            prompt_parts.append(f"{role}:\n{content}")
        prompt = "\n\n".join(prompt_parts)

        if json_schema:
            prompt += (
                "\n\nReturn ONLY a valid JSON object matching this schema:\n"
                f"```json\n{json.dumps(json_schema, indent=2)}\n```"
            )

        schema = json_schema or {}
        logger.info(
            "Codex chat: model=%s, task_id=%s, schema_keys=%s",
            model,
            task_id or "?",
            list(schema.get("properties", {}).keys()) if schema else "none",
        )

        response = codex_bridge.run_turn_json(
            prompt,
            model=model,
            cwd=str(_BASE),
            output_schema=schema,
            timeout=timeout_seconds,
        )

        return LLMResponse(
            content=json.dumps(response) if response else "",
            usage={},
            telemetry={},
            raw=response or {},
        )

    # ── Chat with Tools ──────────────────────────────────────────────────

    async def chat_with_tools(
        self,
        client: httpx.AsyncClient,
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
        """Codex does not natively support tool-calling.

        Falls back to a structured JSON prompt that instructs the model
        to return tool call intents as JSON. In practice, the Implementer
        role should use the GatewayProvider for tool-calling workflows.
        """
        logger.warning(
            "CodexProvider.chat_with_tools called — Codex does not natively "
            "support tool-calling. Falling back to structured prompt."
        )
        # Serialize the tool definitions into the prompt for awareness
        tools_desc = json.dumps(tools, indent=2)
        enhanced_messages = list(messages)
        enhanced_messages.append(
            {
                "role": "user",
                "content": (
                    f"Available tools:\n```json\n{tools_desc}\n```\n\n"
                    "If you need to use any tool, return a JSON object with "
                    "'tool_calls' array containing objects with 'function.name' "
                    "and 'function.arguments'."
                ),
            }
        )
        return await self.chat(
            client,
            enhanced_messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            agent_role=agent_role,
            task_id=task_id,
            task=task,
        )
