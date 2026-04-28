#!/usr/bin/env python3
"""Shared utilities for all agent roles.

Centralizes the AgentResult dataclass, system prompt loading, model
resolution, and JSON schema definitions so each role module can import
a stable API without circular dependencies on ``agent_runner``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

logger = get_logger("roles.common")

HERE = Path(__file__).resolve().parent.parent  # worker-manager/
BASE = HERE.parent  # src/
AGENTS_ROOT = BASE / "agents"

# Ensure worker-manager-local modules win
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(BASE) not in sys.path:
    sys.path.insert(1, str(BASE))


# ── Core Data Structures ────────────────────────────────────────────────


@dataclass
class AgentResult:
    """Unified return type for all agent role functions."""

    action: str
    summary: str
    artifacts: list[str] = field(default_factory=list)
    verdict: Optional[str] = None
    bugs: list[dict[str, Any]] = field(default_factory=list)
    subtasks: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Model Resolution ────────────────────────────────────────────────────


def current_llm_model() -> str:
    """Read the active LLM model from ES config, falling back to env.

    This allows the model to be changed in the Settings UI and take effect
    on the next agent iteration without requiring a container restart.
    """
    try:
        from workspace_llm_env import get_active_llm_model

        return get_active_llm_model()
    except Exception:
        return (os.environ.get("LLM_MODEL") or "llama3.2").strip() or "llama3.2"


# ── System Prompt Loading ───────────────────────────────────────────────


def load_system_prompt(role: str) -> str:
    """Load the system prompt markdown for an agent role."""
    sp = AGENTS_ROOT / role / "system-prompt.md"
    if sp.exists():
        return sp.read_text().strip()
    return f"You are a helpful {role} agent."


# ── JSON Response Schemas ───────────────────────────────────────────────
# These schemas define the expected JSON output structure for each agent
# role. They are passed to the provider via json_schema= to enforce
# structured output.


def json_schema_pm() -> dict[str, Any]:
    """JSON schema for PM Dispatcher role responses."""
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "One of: 'decompose' or 'compute_ready'. Use 'decompose' ONLY for epics/features that need splitting. Use 'compute_ready' for leaf-level tasks.",
            },
            "summary": {
                "type": "string",
                "description": "What you decided and why.",
            },
            "subtasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "objective": {"type": "string"},
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Array of subtask IDs that must complete before this one starts.",
                        },
                    },
                    "required": ["title", "objective"],
                },
                "description": "Only if action='decompose'.",
            },
        },
        "required": ["action", "summary"],
    }


def json_schema_tester() -> dict[str, Any]:
    """JSON schema for Tester role responses."""
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Either 'pass' if tests are satisfactory or 'fail' if bugs were found.",
            },
            "summary": {
                "type": "string",
                "description": "A short summary of the testing outcome.",
            },
            "bugs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "objective": {"type": "string"},
                        "severity": {"type": "string"},
                    },
                    "required": ["title", "objective"],
                },
                "description": "List of bugs found, if action='fail'.",
            },
        },
        "required": ["action", "summary"],
    }


def json_schema_reviewer() -> dict[str, Any]:
    """JSON schema for Reviewer role responses."""
    return {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "description": "Either 'approved' or 'changes_requested'.",
            },
            "summary": {
                "type": "string",
                "description": "A short summary of the review outcome.",
            },
        },
        "required": ["verdict", "summary"],
    }


def json_schema_implementer() -> dict[str, Any]:
    """JSON schema for Implementer role responses."""
    return {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "A short summary of what was implemented.",
            },
            "commit_message": {
                "type": "string",
                "description": "The git commit message for the changes.",
            },
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of file paths that were changed.",
            },
        },
        "required": ["summary", "commit_message"],
    }


# ── Pre-flight Validation ───────────────────────────────────────────────

# Each entry: (keywords_in_title, file_extensions_to_check)
_PHANTOM_ARTIFACT_PATTERNS = [
    (["replace", "svg"], [".svg"]),
    (["replace", "icon", "asset"], [".svg", ".png", ".ico"]),
    (["replace", "image", "asset"], [".png", ".jpg", ".jpeg", ".webp"]),
    (["replace", "png"], [".png"]),
    (["swap", "icon"], [".svg", ".png", ".ico"]),
    (["swap", "image"], [".png", ".jpg", ".jpeg", ".webp"]),
    (["update", "icon", "asset"], [".svg", ".png", ".ico"]),
]


def extract_validation_symbols(text: str) -> list[str]:
    """Extract likely file names and components/functions for AST validation."""
    files = re.findall(
        r"\b[\w\.\-]+\.(?:tsx|ts|js|jsx|py|go|html|css|md|json|yml|yaml)\b",
        text,
        re.IGNORECASE,
    )
    symbols = re.findall(r"\b[A-Z][a-z]+[A-Z][a-zA-Z]*\b", text)
    found = {s.strip(".") for s in files} | set(symbols)
    ignore = {"API", "URL", "UI", "UX", "JSON", "HTML", "HTTP", "REST", "GraphQL", "PDF", "XML"}
    return [s for s in found if s not in ignore]


def preflight_validate_task(
    task: dict,
    repo_path: Optional[str],
    progress_fn: Any = None,
) -> Optional[AgentResult]:
    """Detect phantom tasks that reference non-existent artifacts.

    Returns an AgentResult to skip the task if validation fails,
    or None if the task looks valid and should proceed normally.
    """
    if not repo_path:
        return None
    title = (task.get("title") or "").lower()
    desc = (task.get("description") or task.get("objective") or "").lower()
    combined = f"{title} {desc}"

    for keywords, extensions in _PHANTOM_ARTIFACT_PATTERNS:
        if all(kw in combined for kw in keywords):
            repo = Path(repo_path)
            found = False
            for ext in extensions:
                try:
                    result = subprocess.run(
                        ["find", str(repo), "-name", f"*{ext}", "-type", "f", "-maxdepth", "5"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.stdout.strip():
                        found = True
                        break
                except Exception:
                    found = True  # fail open
                    break
            if not found:
                reason = (
                    f"Pre-flight validation: task assumes {'/'.join(extensions)} artifacts exist "
                    f"but none were found in {repo_path}. Skipping phantom task."
                )
                if progress_fn:
                    progress_fn(f"Skipping: {reason}")
                logger.warning(reason)
                return AgentResult(
                    action="implementation_complete",
                    summary=reason,
                    artifacts=[],
                    metadata={"source": "preflight_skip", "commit_sha": "", "commit_message": ""},
                )

    # ── AST-Aware Task Validation ─────────────────────────────────────
    mod_verbs = ["update", "replace", "modify", "edit", "fix", "change"]
    is_mod_task = any(verb in combined for verb in mod_verbs)
    create_verbs = ["create", "add ", "new ", "implement"]
    is_create_task = any(verb in combined for verb in create_verbs)

    if is_mod_task and not is_create_task:
        symbols = extract_validation_symbols(combined)
        if symbols:
            from tools.executors import exec_elastro_query_ast as _sync_check_ast

            all_missed = True
            for sym in symbols:
                # Use sync wrapper around the async AST query
                ast_result = _sync_check_ast({"query": sym}, repo_path)
                if "No matching nodes found" not in str(ast_result):
                    all_missed = False
                    break
            if all_missed:
                reason = (
                    f"AST-Aware Validation: task aims to modify specific symbols "
                    f"({', '.join(symbols)}) but none were found in the Elastro index for "
                    f"{repo_path}. Skipping phantom task."
                )
                if progress_fn:
                    progress_fn(f"Skipping: {reason}")
                logger.warning(reason)
                return AgentResult(
                    action="implementation_complete",
                    summary=reason,
                    artifacts=[],
                    metadata={"source": "preflight_skip", "commit_sha": "", "commit_message": ""},
                )

    return None


# ── Iteration Limits ────────────────────────────────────────────────────


def implementer_max_iterations() -> int:
    """Return the max tool-calling loop iterations, bounded [5, 80]."""
    raw = os.environ.get("FLUME_IMPLEMENTER_MAX_ITERATIONS", "30").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 30
    return max(5, min(n, 80))
