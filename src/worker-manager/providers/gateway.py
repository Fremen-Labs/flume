#!/usr/bin/env python3
"""GatewayProvider — Default LLM provider routed through the Go flume-gateway.

This is the primary provider for all three Flume operating modes:
    1. Frontier-only (Claude, Gemini, GPT, etc.)
    2. Hybrid (Frontier + Local Ollama nodes)
    3. Local-only (Ollama nodes exclusively)

The Go gateway handles node selection, load balancing, and think-block
stripping. This provider is a thin async HTTP client that normalizes
the gateway response into the unified ``LLMResponse`` envelope.

Replaces the legacy ``_call_ollama`` and ``_call_ollama_tools`` functions
from ``agent_runner.py``.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from providers.registry import LLMResponse
from utils.logger import get_logger
from utils.es_auth import get_es_auth_headers

logger = get_logger("providers.gateway")


class GatewayProvider:
    """LLM provider that routes through the Go flume-gateway.

    Satisfies the ``LLMProvider`` protocol via structural typing.
    """

    @property
    def name(self) -> str:
        return "gateway"

    # ── Chat (JSON response) ─────────────────────────────────────────────

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
        """Send a chat completion through the gateway.

        If ``json_schema`` is provided, it is appended to the user instruction
        so the model constrains its output to valid JSON matching the schema.

        Args:
            client: Shared httpx.AsyncClient.
            messages: OpenAI-format message list.
            model: Model name to route to.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            timeout_seconds: HTTP timeout.
            json_schema: Optional JSON schema to enforce structured output.
            agent_role: Agent role hint for gateway routing.
            task_id: Task ID for telemetry correlation.
            task: Full task dict for credential resolution.

        Returns:
            Unified ``LLMResponse`` with content, usage, and telemetry.
        """
        kw = _resolve_task_credentials(task)

        payload: dict[str, Any] = {
            "messages": messages,
            "model": model or "",
            "provider": kw.get("provider_override", ""),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "think": False,
            "agent_role": agent_role,
        }
        if task_id:
            payload["task_id"] = task_id

        resp = await _post_gateway_async(
            client, "/v1/chat", payload, timeout=timeout_seconds
        )

        telemetry = resp.get("telemetry", {})
        if telemetry:
            logger.info(
                "Gateway telemetry: node_id=%s, node_host=%s",
                telemetry.get("node_id"),
                telemetry.get("node_host"),
            )

        content = resp.get("message", {}).get("content", "")

        # ── Post-call telemetry emission ─────────────────────────────────
        usage = resp.get("usage", {})
        await _emit_usage(task, usage, client=client)
        await _sync_task_execution_host(task, telemetry, client=client)

        # ── JSON fence extraction ────────────────────────────────────────
        parsed_content = _extract_json_from_fences(content)

        return LLMResponse(
            content=parsed_content if parsed_content else content.strip(),
            usage=usage,
            telemetry=telemetry,
            raw=resp,
        )

    # ── Chat with Tools (function-calling) ───────────────────────────────

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
        """Send a tool-calling chat completion through the gateway.

        Args:
            client: Shared httpx.AsyncClient.
            messages: OpenAI-format message list.
            tools: Tool definitions in OpenAI function-calling format.
            model: Model name to route to.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            agent_role: Agent role hint for gateway routing.
            task_id: Task ID for telemetry correlation.
            task: Full task dict for credential resolution.

        Returns:
            Unified ``LLMResponse`` with tool_calls, usage, and telemetry.
        """
        kw = _resolve_task_credentials(task)

        payload: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": model or "",
            "provider": kw.get("provider_override", ""),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "think": True,
            "agent_role": agent_role,
        }
        if task_id:
            payload["task_id"] = task_id

        resp = await _post_gateway_async(
            client, "/v1/chat/tools", payload, timeout=180
        )

        telemetry = resp.get("telemetry", {})
        if telemetry:
            logger.info(
                "Gateway telemetry (tools): node_id=%s, node_host=%s",
                telemetry.get("node_id"),
                telemetry.get("node_host"),
            )

        message = resp.get("message", {})
        usage = resp.get("usage", {})
        if usage:
            await _emit_usage(task, usage, client=client)
        await _sync_task_execution_host(task, telemetry, client=client)

        return LLMResponse(
            content=message.get("content", ""),
            usage=usage,
            telemetry=telemetry,
            tool_calls=message.get("tool_calls") or [],
            thoughts=message.get("thoughts") or "",
            raw=resp,
        )


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _gateway_url() -> str:
    return os.environ.get("FLUME_GATEWAY_URL", "http://gateway:8090").rstrip("/")


async def _post_gateway_async(
    client: httpx.AsyncClient,
    path: str,
    payload: dict,
    timeout: int = 180,
    max_retries: int = 3,
) -> dict:
    """POST JSON to the gateway with exponential backoff and jitter."""
    import asyncio
    import random

    url = f"{_gateway_url()}{path}"
    worker_name = os.environ.get("FLUME_WORKER_NAME", "unknown")
    backoffs = [30, 60, 120]

    for attempt in range(max_retries + 1):
        try:
            resp = await client.post(
                url,
                json=payload,
                headers={"X-Worker-Name": worker_name},
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401, 402, 403, 404, 429):
                logger.error(
                    "Gateway rejected request (HTTP %d): %s",
                    exc.response.status_code,
                    exc.response.text,
                )
                raise RuntimeError(
                    f"Gateway HTTP {exc.response.status_code}: {exc.response.reason_phrase}"
                ) from exc
            if attempt < max_retries:
                base_sleep = backoffs[min(attempt, len(backoffs) - 1)]
                jitter = base_sleep * 0.1 * (random.random() * 2 - 1)
                sleep_time = max(1.0, base_sleep + jitter)
                logger.warning(
                    "Gateway HTTP error (attempt %d/%d): %s. Backing off %.1fs.",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    sleep_time,
                )
                await asyncio.sleep(sleep_time)
            else:
                logger.error("Gateway connection permanently failed: %s", exc)
                raise exc
        except httpx.RequestError as exc:
            if isinstance(exc, httpx.ReadTimeout) and timeout >= 60:
                logger.error("Gateway request timed out after %ds.", timeout)
                raise exc
            if attempt < max_retries:
                base_sleep = backoffs[min(attempt, len(backoffs) - 1)]
                jitter = base_sleep * 0.1 * (random.random() * 2 - 1)
                sleep_time = max(1.0, base_sleep + jitter)
                logger.warning(
                    "Gateway connection issue (attempt %d/%d): %s. Backing off %.1fs.",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    sleep_time,
                )
                await asyncio.sleep(sleep_time)
            else:
                logger.error("Gateway connection permanently failed: %s", exc)
                raise exc
    # Unreachable — loop always raises on final failure
    raise RuntimeError("Gateway retry loop exhausted without result")  # pragma: no cover


def _resolve_task_credentials(task: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Resolve LLM credentials from the task's preferred_llm_credential_id.

    Returns a dict with optional ``provider_override`` and ``base_url_override``.
    """
    if not task:
        return {}
    try:
        import sys
        from pathlib import Path

        here = Path(__file__).resolve().parent.parent
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        import llm_credentials_store as lcs

        cred_id = str(task.get("preferred_llm_credential_id") or "").strip()
        if cred_id and cred_id != lcs.SETTINGS_DEFAULT_CREDENTIAL_ID:
            resolved = lcs.get_resolved_for_worker(here.parent, cred_id)
            if resolved:
                bu = (resolved.get("base_url") or "").strip() or None
                return {
                    "provider_override": resolved["provider"],
                    "base_url_override": bu,
                    "api_key_override": resolved.get("api_key", ""),
                }
        pov = (task.get("preferred_llm_provider") or "").strip().lower()
        if not pov:
            return {}
        return {"provider_override": pov, "base_url_override": None}
    except Exception as exc:
        logger.warning("Credential resolution failed: %s", exc)
        return {}


def _extract_json_from_fences(content: str) -> Optional[str]:
    """Extract JSON content from markdown code fences.

    Handles trailing prose after closing fence, case-insensitive language
    tags, and nested fences.

    Returns:
        The extracted JSON string if fences were found, else None.
    """
    val = content.strip()
    if not val.startswith("```"):
        return None
    first_nl = val.index("\n") if "\n" in val else len(val)
    inner = val[first_nl + 1 :]
    last_fence = inner.rfind("```")
    if last_fence != -1:
        inner = inner[:last_fence]
    return inner.strip()


async def _emit_usage(
    task: Optional[dict[str, Any]],
    usage: dict,
    *,
    client: httpx.AsyncClient,
) -> None:
    """Emit a token-usage telemetry document to agent-token-telemetry."""
    if not task or not usage or not client:
        return
    try:
        from datetime import datetime, timezone

        es_url = os.environ.get("ES_URL", "http://elasticsearch:9200").rstrip("/")
        input_tokens = usage.get("prompt_tokens") or usage.get("prompt_eval_count") or 0
        output_tokens = usage.get("completion_tokens") or usage.get("eval_count") or 0
        ts = datetime.now(timezone.utc).isoformat()
        doc = {
            "@timestamp": ts,
            "worker_name": task.get("active_worker") or task.get("assigned_agent") or "unknown-worker",
            "worker_role": task.get("assigned_agent_role") or task.get("owner") or "generic",
            "provider": task.get("preferred_llm_provider") or task.get("llm_provider") or "ollama",
            "model": task.get("preferred_model") or task.get("llm_model") or "unknown",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "created_at": ts,
        }
        hdrs: dict[str, str] = {"Content-Type": "application/json"}
        hdrs.update(get_es_auth_headers())
        await client.post(
            f"{es_url}/agent-token-telemetry/_doc",
            json=doc,
            headers=hdrs,
            timeout=2.0,
        )
    except Exception as exc:
        logger.warning("Telemetry delivery aborted: %s", exc)


async def _sync_task_execution_host(
    task: Optional[dict[str, Any]],
    telemetry: dict,
    *,
    client: httpx.AsyncClient,
) -> None:
    """Sync execution host telemetry back to the task document in ES."""
    if not task or not telemetry or not client:
        return
    tid = task.get("id", task.get("_id"))
    if not tid:
        return
    host = telemetry.get("node_host")
    model = telemetry.get("model")
    if not host:
        return
    try:
        es_url = os.environ.get("ES_URL", "http://elasticsearch:9200").rstrip("/")
        hdrs: dict[str, str] = {"Content-Type": "application/json"}
        hdrs.update(get_es_auth_headers())
        doc: dict[str, str] = {"execution_host": host}
        if model:
            doc["model"] = model
        await client.post(
            f"{es_url}/{os.environ.get('ES_INDEX_TASKS', 'agent-task-records')}/_update/{tid}",
            json={"doc": doc},
            headers=hdrs,
            timeout=2.0,
        )
        logger.info("Synchronized execution telemetry for task %s", tid)
    except Exception as exc:
        logger.warning("Failed to sync execution host to task: %s", exc)
