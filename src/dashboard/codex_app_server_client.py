from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import codex_app_server


def _send(stdin, obj: dict[str, Any]) -> None:
    stdin.write(json.dumps(obj) + "\n")
    stdin.flush()


def _planner_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "message": {"type": "string"},
            "plan": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "epics": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "features": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "id": {"type": "string"},
                                            "title": {"type": "string"},
                                            "stories": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "additionalProperties": False,
                                                    "properties": {
                                                        "id": {"type": "string"},
                                                        "title": {"type": "string"},
                                                        "acceptanceCriteria": {
                                                            "type": "array",
                                                            "items": {"type": "string"},
                                                        },
                                                        "tasks": {
                                                            "type": "array",
                                                            "items": {
                                                                "type": "object",
                                                                "additionalProperties": False,
                                                                "properties": {
                                                                    "id": {"type": "string"},
                                                                    "title": {"type": "string"},
                                                                },
                                                                "required": ["id", "title"],
                                                            },
                                                        },
                                                    },
                                                    "required": ["id", "title", "acceptanceCriteria", "tasks"],
                                                },
                                            },
                                        },
                                        "required": ["id", "title", "stories"],
                                    },
                                },
                            },
                            "required": ["id", "title", "description", "features"],
                        },
                    }
                },
                "required": ["epics"],
            },
        },
        "required": ["message", "plan"],
    }


def _messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in messages:
        role = str(m.get('role') or 'user').strip().upper()
        content = m.get('content')
        if isinstance(content, list):
            text = json.dumps(content, ensure_ascii=False)
        else:
            text = str(content or '')
        parts.append(f"[{role}]\n{text}")
    parts.append(
        "Return only a JSON object matching the required schema with keys 'message' and 'plan'."
    )
    return "\n\n".join(parts)


def _extract_text_from_notification(msg: dict[str, Any]) -> str:
    params = msg.get('params') or {}
    if not isinstance(params, dict):
        return ''
    for key in ('delta', 'text'):
        val = params.get(key)
        if isinstance(val, str):
            return val
    item = params.get('item')
    if isinstance(item, dict):
        for key in ('text', 'delta'):
            val = item.get(key)
            if isinstance(val, str):
                return val
        content = item.get('content')
        if isinstance(content, list):
            texts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                val = block.get('text')
                if isinstance(val, str):
                    texts.append(val)
            if texts:
                return ''.join(texts)
    return ''


def planner_chat(messages: list[dict[str, Any]], model: str, cwd: str | None = None, timeout: int = 180) -> str:
    # Prefer `codex exec` for compatibility across Codex CLI versions.
    # The previous JSON-RPC app-server handshake differs across versions and can
    # fail before thread/start despite valid auth.
    codex_bin = shutil.which("codex")
    if codex_bin:
        base_cmd = [codex_bin]
    else:
        npx_bin = shutil.which("npx")
        if not npx_bin:
            raise FileNotFoundError("Neither codex nor npx is on PATH")
        base_cmd = [npx_bin, "--yes", "@openai/codex"]

    effective_cwd = cwd or str(Path.cwd())
    prompt = _messages_to_prompt(messages)
    with tempfile.TemporaryDirectory(prefix="flume-codex-") as td:
        tdp = Path(td)
        schema_path = tdp / "planner-schema.json"
        out_path = tdp / "planner-out.txt"
        schema_path.write_text(json.dumps(_planner_output_schema(), ensure_ascii=False), encoding="utf-8")

        cmd = [
            *base_cmd,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "-C",
            effective_cwd,
            "-m",
            model,
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(out_path),
            "-",
        ]

        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
            cwd=effective_cwd or None,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"codex exec failed (exit {proc.returncode}): {err[:500]}")
        if not out_path.exists():
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"codex exec produced no planner output file. {err[:500]}")
        out = out_path.read_text(encoding="utf-8", errors="replace").strip()
        if not out:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"codex exec returned empty planner output. {err[:500]}")
        return out
