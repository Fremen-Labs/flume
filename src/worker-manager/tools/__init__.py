#!/usr/bin/env python3
"""Flume Tool Execution Engine.

Extracts all tool definitions, executor functions, and the dispatch
loop from agent_runner.py into a clean, modular package.

Usage::

    from tools import IMPLEMENTER_TOOLS, ELASTRO_QUERY_TOOL, dispatch_tool_call
"""

from tools.definitions import IMPLEMENTER_TOOLS, ELASTRO_QUERY_TOOL  # noqa: F401
from tools.dispatch import dispatch_tool_call  # noqa: F401
from tools.executors import (  # noqa: F401
    resolve_path,
    exec_read_file,
    exec_write_file,
    exec_elastro_query_ast,
    exec_memory_read,
    exec_memory_write,
    exec_multi_replace_file_content,
    exec_list_directory,
    exec_run_shell,
)

__all__ = [
    "IMPLEMENTER_TOOLS",
    "ELASTRO_QUERY_TOOL",
    "dispatch_tool_call",
    "resolve_path",
    "exec_read_file",
    "exec_write_file",
    "exec_elastro_query_ast",
    "exec_memory_read",
    "exec_memory_write",
    "exec_multi_replace_file_content",
    "exec_list_directory",
    "exec_run_shell",
]
