#!/usr/bin/env python3
"""PM Dispatcher agent role.

Decomposes or approves a task for execution using the ProviderRegistry
for provider-agnostic LLM dispatch.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import httpx

from providers import get_registry
from roles.common import (
    AgentResult,
    current_llm_model,
    json_schema_pm,
    load_system_prompt,
)
from utils.logger import get_logger

logger = get_logger("roles.pm_dispatcher")


# ── Phase 2.3: Enumerable Target Detection ──────────────────────────────────
# Patterns that signal the user prompt contains multiple discrete entities
# that should each become a subtask (e.g., "all commands", "each module").

_ENUMERABLE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\ball\b.*\b(commands?|modules?|endpoints?|services?|files?|components?|pages?|routes?)\b", re.I),
    re.compile(r"\beach\b.*\b(command|module|endpoint|service|file|component|page|route)\b", re.I),
    re.compile(r"\bevery\b.*\b(command|module|endpoint|service|file|component|page|route)\b", re.I),
    re.compile(r"\b(review|update|audit|document|test)\b.*\b(and|to)\b.*\b(ensure|update|verify)\b", re.I),
]


def _estimate_enumerable_targets(task: dict) -> int:
    """Estimate the number of discrete entities referenced in the task description.

    Phase 2.3: Detects enumerable scope in the task title/description so the PM
    can be instructed to decompose rather than treating multi-entity requests as
    a single monolithic task.

    Returns 0 if no enumerable pattern is detected, otherwise a positive count.
    """
    title = (task.get("title") or "").strip()
    desc = (task.get("description") or task.get("objective") or "").strip()
    full_text = f"{title} {desc}"

    for pattern in _ENUMERABLE_PATTERNS:
        if pattern.search(full_text):
            # Return a minimum hint — the LLM will determine the actual count
            return max(2, len(re.findall(r",\s*", full_text)) + 1)
    return 0


def _get_cluster_topology() -> dict[str, Any]:
    """Fetch the current cluster topology from Elasticsearch."""
    from worker_handlers import es_request  # type: ignore

    try:
        res = es_request("/agent-system-workers/_search", {"size": 100}, method="GET")
        implementers = []
        for hit in res.get("hits", {}).get("hits", []):
            state = hit.get("_source", {})
            for w in state.get("workers", []):
                if w.get("role") == "implementer":
                    implementers.append(w)
        models = list({w.get("model", "unknown") for w in implementers})
        return {
            "available_implementers": len(implementers) or 1,
            "target_models": models or ["unknown"],
        }
    except Exception as e:
        logger.warning("Error fetching cluster topology from ES: %s", e)
        return {"available_implementers": 1, "target_models": ["unknown"]}


async def run_pm_dispatcher(
    task: Optional[dict[str, Any]] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> AgentResult:
    """Decompose or approve a task for execution.

    Uses the ProviderRegistry to resolve the correct LLM backend based on
    task configuration. JSON schema enforcement is applied uniformly
    regardless of which provider handles the request.
    """
    logger.info("run_pm_dispatcher: started")
    system_prompt = load_system_prompt("pm-dispatcher")
    topology = _get_cluster_topology()
    logger.info("run_pm_dispatcher: cluster topology=%s", topology)

    # ── Complexity-aware instruction ──────────────────────────────────
    item_type = (task or {}).get("item_type", "task")

    instruction = (
        "You are the Program Manager. Analyze the task and the following cluster "
        "execution topology:\n"
        f"- Available Implementer Nodes: {topology['available_implementers']}\n"
        f"- Target Implementer Models: {', '.join(topology['target_models'])}\n\n"
    )

    if item_type == "task":
        instruction += (
            "This item is already a TASK (leaf-level work item). It has been scoped by "
            "the planner and should NOT be decomposed further.\n"
            "Return action='compute_ready' to push it to the execution queue.\n"
            "Do NOT return action='decompose'. Do NOT create subtasks.\n"
        )
    else:
        # Phase 2.3: Detect enumerable targets and inject mandatory decomposition.
        enumerable_count = _estimate_enumerable_targets(task or {})
        if enumerable_count > 0:
            instruction += (
                "MANDATORY DECOMPOSITION — ENUMERABLE SCOPE DETECTED:\n"
                f"The user's request references approximately {enumerable_count}+ discrete "
                "entities (commands, modules, endpoints, etc.). You MUST decompose this "
                "into individual subtasks — one per entity — so they can be executed in "
                "parallel across the available implementer nodes.\n"
                "Return action='decompose' with at least one subtask per entity.\n"
                "Do NOT return action='compute_ready' for multi-entity requests.\n\n"
            )
            logger.info(
                "run_pm_dispatcher: enumerable scope detected (%d targets), "
                "forcing decomposition directive",
                enumerable_count,
            )

        instruction += (
            "COMPLEXITY-PROPORTIONAL DECOMPOSITION RULES:\n"
            "- If the task title describes a trivial change (URL update, typo fix, config change, "
            "single-file edit), return action='compute_ready' — do NOT decompose.\n"
            "- Only return action='decompose' if the work genuinely requires multiple independent "
            "implementation steps across different files or components.\n"
            "- When decomposing, create the MINIMUM number of subtasks needed. Each subtask must "
            "modify different files or components. Never create separate tasks for 'locate file' "
            "and 'make change'.\n"
            "- CRITICAL: You must explicitly map sequential dependencies using the `depends_on` array. "
            "If a testing or verification task depends on an implementation task, place the "
            "implementation task's ID in the verification task's `depends_on` array.\n"
            "- Never create subtasks that assume artifacts exist without evidence (e.g., "
            "'replace icon asset' when no SVG files were mentioned).\n\n"
        )
        if "gpt-4" not in (str((task or {}).get("preferred_model") or "")).lower():
            instruction += (
                "The Implementer models are local/quantized — if decomposition is needed, "
                "keep subtasks tightly scoped (1 function/file per task) to reduce hallucination.\n"
            )
        else:
            instruction += (
                "The Implementer models are Frontier API models — you may chunk work "
                "into broader architecture scopes if decomposition is needed.\n"
            )

    # ── Provider-agnostic LLM call ───────────────────────────────────
    registry = get_registry()
    provider = registry.resolve(task)
    model = (task or {}).get("preferred_model") or current_llm_model()
    schema = json_schema_pm()

    logger.info("run_pm_dispatcher: using provider=%s, model=%s", provider.name, model)

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(
                {"instruction": instruction, "task": task or {}},
                indent=2,
            ),
        },
    ]

    llm_response = await provider.chat(
        client,
        messages,
        model=model,
        json_schema=schema,
        agent_role="pm-dispatcher",
        task_id=(task or {}).get("id", (task or {}).get("_id")),
        task=task,
    )

    # ── Parse response ───────────────────────────────────────────────
    response: Optional[dict[str, Any]] = None
    content = llm_response.content
    if content:
        try:
            response = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError:
            logger.warning(
                "run_pm_dispatcher: JSON parse failed — raw (first 2000 chars): %s",
                content[:2000],
            )

    logger.info("run_pm_dispatcher: completed via provider=%s", provider.name)

    if response and isinstance(response, dict):
        return AgentResult(
            action=response.get("action", "compute_ready"),
            summary=response.get("summary", "Computed readiness for queued tasks."),
            subtasks=response.get("subtasks") or [],
            metadata={"source": "llm", "provider": provider.name},
        )
    return AgentResult(
        action="compute_ready",
        summary="Computed readiness for queued tasks (fallback).",
        subtasks=[],
        metadata={"source": "fallback"},
    )
