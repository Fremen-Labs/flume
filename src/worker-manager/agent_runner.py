#!/usr/bin/env python3
"""Flume Agent Runner — Thin Facade.

This module serves as the backward-compatible import surface for the refactored
Flume agent architecture. All logic has been decomposed into focused packages:

- ``providers/`` — LLM Provider Abstraction Layer (Phase 1)
- ``roles/``     — Agent Role Decomposition (Phase 2)
- ``tools/``     — Tool Execution Engine (Phase 3)

Handler modules (``handlers/pm.py``, ``handlers/implementer.py``, etc.) import
symbols from ``agent_runner``, so this facade re-exports all public names to
preserve import compatibility during the incremental migration.

**New code should import directly from the sub-packages.**
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx

from utils.logger import get_logger

logger = get_logger(__name__)

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(BASE) not in sys.path:
    sys.path.insert(1, str(BASE))

# ── Re-exports from roles/ ──────────────────────────────────────────────
# These are the primary public API consumed by handler modules.

from roles.common import AgentResult  # noqa: E402, F401
from roles.common import current_llm_model as _current_llm_model  # noqa: E402, F401
from roles.common import load_system_prompt as _load_system_prompt  # noqa: E402, F401
from roles.common import json_schema_pm as _json_schema_pm  # noqa: E402, F401
from roles.common import json_schema_tester as _json_schema_tester  # noqa: E402, F401
from roles.common import json_schema_reviewer as _json_schema_reviewer  # noqa: E402, F401
from roles.common import json_schema_implementer as _json_schema_implementer  # noqa: E402, F401
from roles.common import preflight_validate_task as _preflight_validate_task  # noqa: E402, F401
from roles.common import extract_validation_symbols as _extract_validation_symbols  # noqa: E402, F401
from roles.common import implementer_max_iterations as _implementer_max_iterations  # noqa: E402, F401

from roles.pm_dispatcher import run_pm_dispatcher  # noqa: E402, F401
from roles.implementer import run_implementer  # noqa: E402, F401
from roles.tester import run_tester  # noqa: E402, F401
from roles.reviewer import run_reviewer  # noqa: E402, F401

# ── Re-exports from tools/ ──────────────────────────────────────────────

from tools.definitions import IMPLEMENTER_TOOLS as _IMPLEMENTER_TOOLS  # noqa: E402, F401
from tools.definitions import ELASTRO_QUERY_TOOL as _ELASTRO_QUERY_TOOL  # noqa: E402, F401
from tools.executors import resolve_path as _resolve_path  # noqa: E402, F401
from tools.executors import exec_read_file as _exec_read_file  # noqa: E402, F401
from tools.executors import exec_write_file as _exec_write_file  # noqa: E402, F401
from tools.executors import exec_elastro_query_ast as _exec_elastro_query_ast  # noqa: E402, F401
from tools.executors import exec_memory_read as _exec_memory_read  # noqa: E402, F401
from tools.executors import exec_memory_write as _exec_memory_write  # noqa: E402, F401
from tools.executors import exec_multi_replace_file_content as _exec_multi_replace_file_content  # noqa: E402, F401
from tools.executors import exec_list_directory as _exec_list_directory  # noqa: E402, F401
from tools.executors import exec_run_shell as _exec_run_shell  # noqa: E402, F401
from tools.executors import tool_result_modified_repo as _tool_result_modified_repo  # noqa: E402, F401

# ── Re-exports from providers/ ──────────────────────────────────────────

from providers import get_registry  # noqa: E402, F401
from providers.registry import LLMResponse  # noqa: E402, F401


# ── Backward-compatible re-exports ──────────────────────────────────────
# These symbols are imported by handler modules (handlers/pm.py, etc.) from
# agent_runner. They are defined in worker_handlers.py but must be importable
# from here to preserve handler compatibility.


def _get_active_llm_model(default: str = "llama3.2") -> str:
    """Re-export: resolve active LLM model from workspace config or env."""
    try:
        from workspace_llm_env import get_active_llm_model

        return get_active_llm_model(default)
    except Exception:
        return (os.environ.get("LLM_MODEL") or default).strip() or default


async def _run_with_client(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Re-export: run an async agent function with a fresh httpx.AsyncClient."""
    async with httpx.AsyncClient() as client:
        kwargs["client"] = client
        return await func(*args, **kwargs)
