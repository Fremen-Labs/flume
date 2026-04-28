#!/usr/bin/env python3
"""Tool definitions for the Implementer agent.

All OpenAI function-calling tool schemas are defined here. Adding a new tool
requires only a new dict entry here and a corresponding executor function in
``tools/executors.py``.
"""

from typing import Any

# ── Elastro AST Query Tool ───────────────────────────────────────────────

ELASTRO_QUERY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "elastro_query_ast",
        "description": (
            "Query the Elastro AST index for precise code mappings and snippets "
            "matching your work item. MUST be used before modifying code to "
            "dynamically save tokens contextually."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query, e.g., a function name or class.",
                },
                "target_path": {
                    "type": "string",
                    "description": "The absolute path to the target repository.",
                },
            },
            "required": ["query", "target_path"],
        },
    },
}

# ── Core Implementer Tool Catalog ────────────────────────────────────────

IMPLEMENTER_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or repo-relative file path",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write (overwrite) a file with the given content. "
                "Creates parent directories as needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or repo-relative file path",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content to write",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_read",
            "description": "Retrieve cached context natively from the semantic memory bounds.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "enum": ["agent_semantic_memory", "agent_knowledge"],
                    },
                    "key": {"type": "string"},
                },
                "required": ["namespace", "key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_write",
            "description": "Persist operational logic natively into semantic memory bounds.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "enum": ["agent_semantic_memory", "agent_knowledge"],
                    },
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "ttl": {
                        "type": "integer",
                        "description": "Time to live in seconds",
                    },
                },
                "required": ["namespace", "key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_replace_file_content",
            "description": (
                "Replace multiple non-contiguous chunks of text in a file. "
                "Use this for deterministic surgical code edits instead of raw bash loops."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or repo-relative file path",
                    },
                    "replacements": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target_content": {
                                    "type": "string",
                                    "description": "Exact string to find",
                                },
                                "replacement_content": {
                                    "type": "string",
                                    "description": "Exact string to replace it with",
                                },
                            },
                            "required": ["target_content", "replacement_content"],
                        },
                    },
                },
                "required": ["path", "replacements"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List files and subdirectories at a path (non-recursive). "
                "Use this instead of shell `ls` or `ls -R`. For deeper trees, "
                "call it on subdirectories or use run_shell with `find`/`grep`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path (defaults to repo root)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Full bash orchestration endpoint. You are empowered to use "
                "apt-get, wget, curl, and pipe operators to natively provision "
                "missing system frameworks or project dependencies automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                    "working_dir": {
                        "type": "string",
                        "description": "Optional working directory override",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "implementation_complete",
            "description": "Signal that all code changes are done and ready for testing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "What was implemented",
                    },
                    "commit_message": {
                        "type": "string",
                        "description": "Git commit message",
                    },
                    "artifacts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of files changed",
                    },
                },
                "required": ["summary", "commit_message"],
            },
        },
    },
]
