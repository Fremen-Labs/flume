#!/usr/bin/env python3
"""Tool dispatch — routes tool calls from the LLM to executor functions.

Replaces the monolithic ``if/elif`` chain in ``run_implementer`` with a
clean dispatch table.  Adding a new tool requires only:
    1. A new entry in ``tools/definitions.py``
    2. A new executor in ``tools/executors.py``
    3. A new entry in ``_DISPATCH_TABLE`` below
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from tools.executors import (
    exec_elastro_query_ast,
    exec_list_directory,
    exec_memory_read,
    exec_memory_write,
    exec_multi_replace_file_content,
    exec_read_file,
    exec_run_shell,
    exec_write_file,
    tool_result_modified_repo,
)
from utils.logger import get_logger

logger = get_logger("tools.dispatch")


async def dispatch_tool_call(
    fn_name: str,
    fn_args: dict[str, Any],
    *,
    repo_path: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    progress_fn: Optional[Any] = None,
) -> tuple[str, bool]:
    """Dispatch a single tool call to its executor.

    Args:
        fn_name: The tool function name from the LLM response.
        fn_args: The parsed arguments dict.
        repo_path: The repository working directory.
        client: Shared httpx.AsyncClient for ES/network calls.
        progress_fn: Optional callback for human-readable progress notes.

    Returns:
        A tuple of (tool_result_str, repo_was_modified).
    """
    repo_modified = False

    def _progress(note: str) -> None:
        if progress_fn:
            try:
                progress_fn(note)
            except Exception:
                pass

    if fn_name == "read_file":
        _progress(f'Reading file: {fn_args.get("path", "")}')
        result = exec_read_file(fn_args, repo_path)

    elif fn_name == "write_file":
        _progress(f'Writing file: {fn_args.get("path", "")}')
        result = exec_write_file(fn_args, repo_path)
        if tool_result_modified_repo(fn_name, result):
            repo_modified = True

    elif fn_name == "multi_replace_file_content":
        _progress(f'Replacing content in: {fn_args.get("path", "")}')
        result = exec_multi_replace_file_content(fn_args, repo_path)
        if tool_result_modified_repo(fn_name, result):
            repo_modified = True

    elif fn_name == "list_directory":
        _progress(f'Listing directory: {fn_args.get("path", "") or "(repo root)"}')
        result = exec_list_directory(fn_args, repo_path)

    elif fn_name == "run_shell":
        cmd = (fn_args.get("command", ""))[:80]
        _progress(f"Running: {cmd}")
        result = exec_run_shell(fn_args, repo_path)

    elif fn_name == "memory_read":
        _progress(f'Reading memory: {fn_args.get("namespace", "")}/{fn_args.get("key", "")}')
        result = await exec_memory_read(fn_args, client=client)

    elif fn_name == "memory_write":
        _progress(f'Writing memory: {fn_args.get("namespace", "")}/{fn_args.get("key", "")}')
        result = await exec_memory_write(fn_args, client=client)

    elif fn_name == "elastro_query_ast":
        _progress(f'Querying AST for nodes mapping: {fn_args.get("query", "")}')
        result = await exec_elastro_query_ast(fn_args, repo_path, client=client)

    elif fn_name == "implementation_complete":
        # Handled by the caller (run_implementer) — not dispatched here
        result = json.dumps({"status": "error", "message": "implementation_complete must be handled by the agent role"})

    else:
        result = f"Unknown tool: {fn_name}"
        _progress(f"Unknown tool called: {fn_name}")

    return result, repo_modified
