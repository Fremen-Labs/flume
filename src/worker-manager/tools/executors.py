#!/usr/bin/env python3
"""Tool executor functions for the Implementer agent.

Each executor maps 1:1 to a tool definition in ``tools/definitions.py``.
All executors follow the same contract:

    def exec_<tool_name>(args: dict, repo_path: Optional[str]) -> str

Async executors additionally accept ``client: httpx.AsyncClient``.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from utils.es_auth import get_es_auth_headers
from utils.logger import get_logger

logger = get_logger("tools.executors")


# ── Path Resolution ──────────────────────────────────────────────────────


def resolve_path(path: str, repo_path: Optional[str]) -> Path:
    """Resolve a file path relative to the repo root with traversal protection.

    Strips legacy ephemeral clone paths (``/tmp/flume-*``) to allow smooth
    resumption across worker claims.

    Raises:
        PermissionError: If the resolved path escapes the repo sandbox.
    """
    path_str = str(path)
    # Automatically strip legacy ephemeral clone paths from the agent's memory
    if path_str.startswith("/tmp/flume-"):
        parts = path_str.split("/")
        if len(parts) >= 4 and parts[1] == "tmp" and parts[2].startswith("flume-"):
            path_str = "/".join(parts[3:])

    p = Path(path_str)
    try:
        base = Path(repo_path).resolve() if repo_path else Path(".").resolve()
        final_path = (base / p).resolve() if not p.is_absolute() else p.resolve()
        if not str(final_path).startswith(str(base)):
            raise PermissionError("Path Traversal Attempt Halted.")
        return final_path
    except Exception:
        raise PermissionError("Path Traversal Attempt Halted.")


# ── File I/O ─────────────────────────────────────────────────────────────


def exec_read_file(args: dict, repo_path: Optional[str]) -> str:
    """Read file contents with truncation for large files."""
    try:
        p = resolve_path(args.get("path", ""), repo_path)
        content = p.read_text(errors="replace")
        if len(content) > 12000:
            return content[:12000] + f"\n... (truncated, {len(content)} total chars)"
        return content
    except Exception as e:
        return f"ERROR reading file: {e}"


def exec_write_file(args: dict, repo_path: Optional[str]) -> str:
    """Write file with Python syntax pre-validation for .py files."""
    try:
        p = resolve_path(args.get("path", ""), repo_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        content = args.get("content", "")
        if p.name.endswith(".py"):
            try:
                compile(content, p.name, "exec")
            except SyntaxError as e:
                return (
                    f"ERROR writing file: Meta-Critic Python Syntax Check Failed "
                    f"at line {e.lineno}: {e.msg}"
                )
        p.write_text(content)
        return f"OK: wrote {len(content)} chars to {p}"
    except Exception as e:
        return f"ERROR writing file: {e}"


def exec_multi_replace_file_content(args: dict, repo_path: Optional[str]) -> str:
    """Apply multiple deterministic find-and-replace operations to a file."""
    try:
        p = resolve_path(args.get("path", ""), repo_path)
        if not p.exists():
            return json.dumps(
                {"status": "error", "message": f"File {p} does not exist", "path": str(p)}
            )

        content = p.read_text(errors="replace")
        replacements = args.get("replacements", [])

        if not replacements:
            return json.dumps({"status": "error", "message": "No replacements provided."})

        for idx, repl in enumerate(replacements):
            target = repl.get("target_content", "")
            new_text = repl.get("replacement_content", "")

            if target not in content:
                return json.dumps(
                    {
                        "status": "error",
                        "message": "target_content not found in file.",
                        "block_index": idx,
                    }
                )
            if content.count(target) > 1:
                return json.dumps(
                    {
                        "status": "error",
                        "message": "target_content matches multiple locations. Make it more specific.",
                        "block_index": idx,
                    }
                )

            content = content.replace(target, new_text)

        p.write_text(content)

        return json.dumps(
            {
                "status": "success",
                "message": f"Applied {len(replacements)} deterministic replacements to {p}",
            }
        )
    except Exception as e:
        logger.exception("Unexpected error in multi_replace_file_content")
        return json.dumps(
            {"status": "error", "message": str(e), "error_type": type(e).__name__}
        )


# ── Directory Listing ────────────────────────────────────────────────────


def exec_list_directory(args: dict, repo_path: Optional[str]) -> str:
    """List files and subdirectories at a path (non-recursive)."""
    try:
        raw = args.get("path") or repo_path or "."
        p = resolve_path(raw, repo_path)
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        lines = [f'{"[d]" if e.is_dir() else "[f]"} {e.name}' for e in entries]
        return "\n".join(lines) if lines else "(empty directory)"
    except Exception as e:
        return f"ERROR listing directory: {e}"


# ── Shell Execution ──────────────────────────────────────────────────────


def exec_run_shell(args: dict, repo_path: Optional[str]) -> str:
    """Execute a shell command in a sandboxed environment.

    Sanitizes credentials, isolates HOME, and enforces a 300s timeout.
    """
    command = args.get("command", "")
    cwd = args.get("working_dir") or repo_path or "."

    # 1. Ephemeral Sandbox Workspace Resolution
    resolved_cwd = resolve_path(cwd, repo_path)
    isolated_home = resolve_path(".cache", repo_path)
    isolated_home.mkdir(parents=True, exist_ok=True)

    # 2. Strict Credential Sanitization & Global Mapping
    safe_env = os.environ.copy()
    for secret in [
        "FLUME_ADMIN_TOKEN",
        "OPENBAO_TOKEN",
        "ADO_PERSONAL_ACCESS_TOKEN",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
    ]:
        safe_env.pop(secret, None)

    safe_env["HOME"] = str(isolated_home)
    safe_env["CARGO_HOME"] = str(isolated_home / ".cargo")
    safe_env["GOPATH"] = str(isolated_home / "go")
    safe_env["NVM_DIR"] = str(isolated_home / ".nvm")
    # Prepend dynamic paths so instantly-installed binaries are available
    safe_env["PATH"] = (
        f"{isolated_home / '.cargo' / 'bin'}:"
        f"{isolated_home / '.local' / 'bin'}:"
        f"{safe_env.get('PATH', '')}"
    )

    try:
        if not command.strip():
            return json.dumps({"status": "error", "message": "Empty command provided."})

        result = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(resolved_cwd),
            env=safe_env,
        )
        output = (result.stdout + result.stderr).strip()
        if len(output) > 6000:
            output = output[:6000] + "\n... (truncated)"
        return json.dumps(
            {
                "status": "success" if result.returncode == 0 else "error",
                "exit_code": result.returncode,
                "output": output or "(no output)",
            }
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"status": "error", "message": "Command timed out after 300s"})
    except Exception as e:
        logger.error(
            {
                "event": "run_shell_error",
                "command": command,
                "error_type": type(e).__name__,
                "error_message": str(e),
            },
            exc_info=True,
        )
        return json.dumps(
            {
                "status": "error",
                "message": f"Execution failed: {e}",
                "error_type": type(e).__name__,
            }
        )


# ── Elastro AST Query ───────────────────────────────────────────────────


async def exec_elastro_query_ast(
    args: dict,
    repo_path: Optional[str],
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Query the Elastro AST index for code mappings and snippets."""
    query = args.get("query", "")
    # Execute the resolve path side effect for validation
    resolve_path(args.get("target_path", repo_path or "."), repo_path)
    if not client:
        return "ERROR: exec_elastro_query_ast missing httpx client"

    try:
        es_url = os.environ.get("ES_URL", "http://elasticsearch:9200").rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        headers.update(get_es_auth_headers())

        query_payload = {
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": [
                        "content",
                        "functions_defined^3",
                        "functions_called^2",
                        "file_path^2",
                        "chunk_name^3",
                        "repo_name",
                    ],
                }
            },
            "size": 12,
            "_source": [
                "file_path",
                "content",
                "functions_defined",
                "functions_called",
                "chunk_type",
                "chunk_name",
                "extension",
                "repo_name",
            ],
        }

        elastro_index = os.environ.get("FLUME_ELASTRO_INDEX", "flume-elastro-graph")

        # ── Token accounting defaults ──────────────────────────────────
        actual_tokens_sent = 0
        baseline_tokens = 0
        baseline_full_context_tokens = 0
        savings = 0

        try:
            resp = await client.post(
                f"{es_url}/{elastro_index}/_search",
                json=query_payload,
                headers=headers,
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                output = (
                    f"AST Search: No matching nodes found for '{query}' in index "
                    f"'{elastro_index}'. Try a broader search term or fall back to "
                    "list_directory + grep."
                )
            else:
                output_chunks = []
                blast_radius_files: set[str] = set()
                total_stored_bytes = 0
                for h in hits:
                    src = h.get("_source", {})
                    fp = src.get("file_path", "unknown")
                    chunk_type = src.get("chunk_type", "file")
                    chunk_name = src.get("chunk_name", "module")
                    ext = src.get("extension", "")
                    fns_defined = src.get("functions_defined", [])
                    fns_called = src.get("functions_called", [])
                    content = src.get("content", "")[:800]

                    entry = f"── {fp} ({chunk_type}: {chunk_name}) [{ext}]"
                    if fns_defined:
                        entry += f"\n  Defines: {', '.join(fns_defined[:10])}"
                    if fns_called:
                        entry += f"\n  Calls: {', '.join(fns_called[:10])}"
                    entry += f"\n  Content:\n{content}"
                    output_chunks.append(entry)

                    if fp and fp not in blast_radius_files:
                        blast_radius_files.add(fp)
                        total_stored_bytes += len(content.encode("utf-8"))

                output = (
                    f"AST Search Results ({len(hits)} hits):\n\n"
                    + "\n\n".join(output_chunks)
                )

                # ── Realistic Token Savings Accounting ─────────────────
                actual_tokens_sent = len(output.encode("utf-8")) // 4
                task_prompt_tokens = len(json.dumps(args).encode("utf-8")) // 4
                num_chunks = len(hits)
                avg_chunk_tokens = 768
                summary_tokens = 512
                baseline_tokens = (
                    task_prompt_tokens + (num_chunks * avg_chunk_tokens) + summary_tokens
                )
                baseline_full_context_tokens = max(
                    total_stored_bytes * 5 // 4, baseline_tokens
                )
                savings = max(baseline_tokens - actual_tokens_sent, 0)

                logger.debug(
                    "ast_telemetry: query='%s' hits=%d actual=%d baseline_rag=%d "
                    "baseline_full=%d savings=%d",
                    query,
                    len(hits),
                    actual_tokens_sent,
                    baseline_tokens,
                    baseline_full_context_tokens,
                    savings,
                )

        except httpx.HTTPStatusError as he:
            if he.response.status_code == 404:
                return (
                    f"AST Search Failed: Index '{elastro_index}' not found. "
                    "The codebase AST has not been ingested yet. Please fall back "
                    "to manual recursive file search via list_directory and grep."
                )
            return (
                f"AST Search HTTP Error: "
                f"{he.response.status_code} {he.response.reason_phrase}"
            )
        except httpx.RequestError as re_err:
            return f"AST Search HTTP Error: {re_err}"

        # Submit agent telemetry metric
        if es_url:
            ts = datetime.now(timezone.utc).isoformat()
            doc = {
                "@timestamp": ts,
                "worker_name": "implementer",
                "worker_role": "system",
                "provider": "elastro-cache",
                "model": "ast-sync",
                "input_tokens": 0,
                "output_tokens": 0,
                "savings": savings,
                "baseline_tokens": baseline_tokens,
                "baseline_full_context_tokens": baseline_full_context_tokens,
                "actual_tokens_sent": actual_tokens_sent,
                "created_at": ts,
            }
            tel_hdrs: dict[str, str] = {"Content-Type": "application/json"}
            tel_hdrs.update(get_es_auth_headers())
            try:
                await client.post(
                    f"{es_url}/agent-token-telemetry/_doc",
                    json=doc,
                    headers=tel_hdrs,
                    timeout=3.0,
                )
            except Exception:
                pass

        return output
    except Exception as e:
        return f"ERROR querying AST natively: {e}"


# ── Semantic Memory ──────────────────────────────────────────────────────


async def exec_memory_read(
    args: dict, client: Optional[httpx.AsyncClient] = None
) -> str:
    """Retrieve cached context from the semantic memory store."""
    ns = args.get("namespace")
    key = args.get("key")

    if not ns or not key:
        logger.error(
            {"event": "memory_read", "status": "failure", "error": "namespace and key are required"}
        )
        return json.dumps({"status": "error", "message": "namespace and key are required"})

    if not client:
        return json.dumps(
            {"status": "error", "message": "exec_memory_read missing httpx client"}
        )

    try:
        es_url = os.environ.get("ES_URL", "https://localhost:9200").rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        headers.update(get_es_auth_headers())
        query_payload = {"query": {"term": {"_id": key}}}

        resp = await client.post(
            f"{es_url}/{ns}/_search",
            json=query_payload,
            headers=headers,
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json()

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            logger.info({"event": "memory_read", "status": "not_found", "namespace": ns, "key": key})
            return json.dumps({"status": "not_found", "message": "No memory stored at this key."})

        src = hits[0].get("_source", {})
        expires = src.get("expires_at")
        if expires:
            if time.time() > expires:
                logger.info(
                    {"event": "memory_read", "status": "expired", "namespace": ns, "key": key}
                )
                return json.dumps(
                    {"status": "not_found", "message": "Memory has expired due to TTL decay."}
                )

        value = src.get("value", "")
        logger.info({"event": "memory_read", "status": "success", "namespace": ns, "key": key})
        return json.dumps({"status": "success", "value": value})

    except httpx.RequestError as e:
        logger.error(
            {
                "event": "memory_read",
                "status": "failure",
                "namespace": ns,
                "key": key,
                "error": str(e),
                "error_type": "RequestError",
            },
            exc_info=True,
        )
        return json.dumps(
            {
                "status": "error",
                "message": f"Network error contacting Elasticsearch: {e}",
                "error_type": "RequestError",
            }
        )

    except Exception as e:
        logger.error(
            {
                "event": "memory_read",
                "status": "failure",
                "namespace": ns,
                "key": key,
                "error": str(e),
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return json.dumps(
            {
                "status": "error",
                "message": f"Internal error during memory read: {e}",
                "error_type": type(e).__name__,
            }
        )


async def exec_memory_write(
    args: dict, client: Optional[httpx.AsyncClient] = None
) -> str:
    """Persist operational logic into the semantic memory store."""
    ns = args.get("namespace")
    key = args.get("key")
    val = args.get("value")
    ttl = args.get("ttl")

    if not ns or not key or not val:
        logger.error(
            {
                "event": "memory_write",
                "status": "failure",
                "error": "namespace, key, and value are required",
            }
        )
        return json.dumps(
            {"status": "error", "message": "namespace, key, and value are required"}
        )

    if not client:
        return json.dumps(
            {"status": "error", "message": "exec_memory_write missing httpx client"}
        )

    try:
        es_url = os.environ.get("ES_URL", "https://localhost:9200").rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        headers.update(get_es_auth_headers())

        doc: dict[str, Any] = {"key": key, "value": val, "updated_at": time.time()}
        if ttl:
            doc["expires_at"] = time.time() + int(ttl)

        safe_key = urllib.parse.quote(key, safe="")

        resp = await client.put(
            f"{es_url}/{ns}/_doc/{safe_key}",
            json=doc,
            headers=headers,
            timeout=5.0,
        )
        resp.raise_for_status()

        logger.info({"event": "memory_write", "status": "success", "namespace": ns, "key": key})
        return json.dumps(
            {"status": "success", "message": f"Wrote memory key {key} to {ns}"}
        )

    except httpx.RequestError as e:
        logger.error(
            {
                "event": "memory_write",
                "status": "failure",
                "namespace": ns,
                "key": key,
                "error": str(e),
                "error_type": "RequestError",
            },
            exc_info=True,
        )
        return json.dumps(
            {
                "status": "error",
                "message": f"Network error contacting Elasticsearch: {e}",
                "error_type": "RequestError",
            }
        )

    except Exception as e:
        logger.error(
            {
                "event": "memory_write",
                "status": "failure",
                "namespace": ns,
                "key": key,
                "error": str(e),
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return json.dumps(
            {
                "status": "error",
                "message": f"Internal error during memory write: {e}",
                "error_type": type(e).__name__,
            }
        )


# ── Helpers ──────────────────────────────────────────────────────────────


def tool_result_modified_repo(fn_name: str, tool_result: str) -> bool:
    """True if write_file or multi_replace actually changed the working tree."""
    if fn_name == "write_file":
        return tool_result.startswith("OK:")
    if fn_name == "multi_replace_file_content":
        try:
            j = json.loads(tool_result)
            return j.get("status") == "success"
        except Exception:
            return False
    return False
