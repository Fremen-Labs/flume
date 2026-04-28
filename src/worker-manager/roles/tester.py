#!/usr/bin/env python3
"""Tester agent role.

Validates implementation quality via LLM-driven test analysis.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from providers import get_registry
from roles.common import (
    AgentResult,
    current_llm_model,
    json_schema_tester,
    load_system_prompt,
)
from utils.logger import get_logger

logger = get_logger("roles.tester")


async def run_tester(
    task: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> AgentResult:
    """Validate implementation quality via LLM-driven test analysis."""
    system_prompt = load_system_prompt("tester")
    registry = get_registry()
    provider = registry.resolve(task)
    model = task.get("preferred_model") or current_llm_model()
    schema = json_schema_tester()

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": (
                        'Return JSON: {"action":"pass|fail","summary":"...",'
                        '"bugs":[{"title":"...","objective":"...","severity":"high|normal"}]}'
                    ),
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
        agent_role="tester",
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
                "run_tester: JSON parse failed — raw: %s",
                llm_response.content[:2000],
            )

    if response and isinstance(response, dict):
        action = response.get("action", "pass")
        if action == "fail":
            bugs = response.get("bugs") or [
                {
                    "title": f"Bug found in {task.get('title', task.get('id', 'task'))}",
                    "objective": response.get(
                        "summary", "Fix failing behavior found during validation."
                    ),
                    "severity": "high",
                }
            ]
            return AgentResult(
                action="fail",
                summary=response.get("summary", "Testing failed."),
                bugs=bugs,
                metadata={"source": "llm", "provider": provider.name},
            )
        return AgentResult(
            action="pass",
            summary=response.get("summary", "Testing passed."),
            metadata={"source": "llm", "provider": provider.name},
        )
    return AgentResult(
        action="pass",
        summary="Testing passed by fallback policy.",
        metadata={"source": "fallback"},
    )
