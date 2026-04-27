from __future__ import annotations

import json
import urllib.error
from utils.exceptions import SAFE_EXCEPTIONS
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)


def _send(stdin, obj: dict[str, Any]) -> None:
    stdin.write(json.dumps(obj) + "\n")
    stdin.flush()


def _planner_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "plan": {
                "type": "object",
                "properties": {
                    "epics": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "features": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"},
                                            "title": {"type": "string"},
                                            "stories": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
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
                                                                "properties": {
                                                                    "id": {"type": "string"},
                                                                    "title": {"type": "string"},
                                                                },
                                                                "required": ["id", "title"],
                                                                "additionalProperties": True,
                                                            },
                                                        },
                                                    },
                                                    "required": ["id", "title", "acceptanceCriteria", "tasks"],
                                                    "additionalProperties": True,
                                                },
                                            },
                                        },
                                        "required": ["id", "title", "stories"],
                                        "additionalProperties": True,
                                    },
                                },
                            },
                            "required": ["id", "title", "description", "features"],
                            "additionalProperties": True,
                        },
                    }
                },
                "required": ["epics"],
                "additionalProperties": True,
            },
        },
        "required": ["message", "plan"],
        "additionalProperties": False,
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


def planner_chat(messages: list[dict[str, Any]], model: str, cwd: str | None = None, timeout: int = 180) -> str:
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
        
        logger.info(json.dumps({"event": "codex_planner_exec_start", "model": model, "cwd": effective_cwd}))

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
            logger.error(json.dumps({"event": "codex_planner_exec_error", "exit_code": proc.returncode, "error": err[:500]}))
            raise RuntimeError(f"codex exec failed (exit {proc.returncode}): {err[:500]}")
        if not out_path.exists():
            err = (proc.stderr or proc.stdout or "").strip()
            logger.error(json.dumps({"event": "codex_planner_exec_no_output_file", "error": err[:500]}))
            raise RuntimeError(f"codex exec produced no planner output file. {err[:500]}")
        out = out_path.read_text(encoding="utf-8", errors="replace").strip()
        if not out:
            err = (proc.stderr or proc.stdout or "").strip()
            logger.error(json.dumps({"event": "codex_planner_exec_empty_output", "error": err[:500]}))
            raise RuntimeError(f"codex exec returned empty planner output. {err[:500]}")
        return out
