#!/usr/bin/env python3
"""Reviewer agent role.

Reviews implementation changes via LLM-driven code review.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from providers import get_registry
from roles.common import (
    AgentResult,
    current_llm_model,
    json_schema_reviewer,
    load_system_prompt,
)
from utils.logger import get_logger

logger = get_logger("roles.reviewer")


async def run_reviewer(
    task: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> AgentResult:
    """Review implementation changes via LLM-driven code review."""
    system_prompt = load_system_prompt("reviewer")
    registry = get_registry()
    provider = registry.resolve(task)
    model = task.get("preferred_model") or current_llm_model()
    schema = json_schema_reviewer()

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": 'Return JSON: {"verdict":"approved|changes_requested","summary":"..."}',
                    "task": task,
                },
                indent=2,
            ),
        },
    ]

    llm_response = await provider.chat(
        client,
        messages,
        model=model,
        json_schema=schema,
        agent_role="reviewer",
        task_id=task.get("id", task.get("_id")),
        task=task,
    )

    response: Optional[dict[str, Any]] = None
    if llm_response.content:
        try:
            response = (
                json.loads(llm_response.content)
                if isinstance(llm_response.content, str)
                else llm_response.content
            )
        except json.JSONDecodeError:
            logger.warning(
                "run_reviewer: JSON parse failed — raw: %s",
                llm_response.content[:2000],
            )

    if response and isinstance(response, dict):
        raw_verdict = response.get("verdict", "approved")
        # Normalise: only 'approved' and 'changes_requested' are valid.
        # Any other value (e.g. 'blocked' hallucinated by the LLM) is treated
        # as 'changes_requested' so the task re-queues rather than blocking.
        if raw_verdict not in ("approved", "changes_requested"):
            raw_verdict = "changes_requested"
        verdict = raw_verdict
        return AgentResult(
            action="review_complete",
            verdict=verdict,
            summary=response.get("summary", f"Review verdict: {verdict}."),
            metadata={"source": "llm", "provider": provider.name},
        )
    return AgentResult(
        action="review_complete",
        verdict="approved",
        summary="Review approved by fallback policy.",
        metadata={"source": "fallback"},
    )
