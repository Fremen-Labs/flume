from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_LISTEN_URL = 'stdio://'
DEFAULT_MODEL = 'gpt-5-codex'

CODEX_BIN_DEFAULT = 'codex'
NPX_BIN = 'npx'
CODEX_NPM_PACKAGE = '@openai/codex'

CMD_APP_SERVER = 'app-server'
CMD_EXEC = 'exec'
CMD_LISTEN = '--listen'
CMD_EPHEMERAL = '--ephemeral'
CMD_SKIP_GIT = '--skip-git-repo-check'
CMD_CWD = '-C'
CMD_MODEL = '-m'
CMD_SANDBOX = '--sandbox'
SANDBOX_MODE = 'workspace-write'
CMD_OUTPUT_SCHEMA = '--output-schema'
CMD_OUTPUT_LAST_MSG = '--output-last-message'

EVENT_EXEC_START = "codex_exec_start"
EVENT_EXEC_FALLBACK = "codex_exec_fallback"
EVENT_EXEC_ERROR = "codex_exec_error"
EVENT_EXEC_NO_OUT = "codex_exec_no_output_file"
EVENT_EXEC_EMPTY_OUT = "codex_exec_empty_output"


class CodexRunConfig(BaseModel):
    prompt: str
    model: str
    cwd: str
    output_schema: dict[str, Any]
    timeout: int = 300


def _which(name: str) -> str | None:
    p = shutil.which(name)
    return p if p else None


def launch_args(extra_args: list[str] | None = None) -> list[str]:
    extra = list(extra_args or [])
    codex_bin = (os.environ.get('FLUME_CODEX_BIN') or CODEX_BIN_DEFAULT).strip() or CODEX_BIN_DEFAULT
    codex_path = _which(codex_bin)
    if codex_path:
        return [codex_path, CMD_APP_SERVER, CMD_LISTEN, DEFAULT_LISTEN_URL, *extra]
    npx_path = _which(NPX_BIN)
    if npx_path:
        return [npx_path, '--yes', CODEX_NPM_PACKAGE, CMD_APP_SERVER, CMD_LISTEN, DEFAULT_LISTEN_URL, *extra]
    raise FileNotFoundError('Neither codex nor npx is on PATH')


def codex_auth_present() -> bool:
    return (Path.home() / '.codex' / 'auth.json').is_file()


def codex_available() -> bool:
    return bool(_which((os.environ.get('FLUME_CODEX_BIN') or CODEX_BIN_DEFAULT).strip() or CODEX_BIN_DEFAULT) or _which(NPX_BIN))


def _extract_json_object(text: str) -> dict[str, Any] | None:
    s = (text or '').strip()
    if not s:
        return None
    # Fast path: entire payload is JSON.
    try:
        val = json.loads(s)
        if isinstance(val, dict):
            return val
    except Exception:
        pass
    # Fallback: parse the last JSON-looking line.
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if not (ln.startswith('{') and ln.endswith('}')):
            continue
        try:
            val = json.loads(ln)
            if isinstance(val, dict):
                return val
        except Exception:
            continue
    # Broad fallback: search for any balanced-ish JSON object region.
    opens = [i for i, ch in enumerate(s) if ch == '{']
    closes = [i for i, ch in enumerate(s) if ch == '}']
    for i in reversed(opens):
        for j in closes:
            if j <= i:
                continue
            frag = s[i:j + 1]
            try:
                val = json.loads(frag)
                if isinstance(val, dict):
                    return val
            except Exception:
                continue
    return None


def _build_base_cmd() -> list[str]:
    codex_bin = (os.environ.get('FLUME_CODEX_BIN') or CODEX_BIN_DEFAULT).strip() or CODEX_BIN_DEFAULT
    codex_path = _which(codex_bin)
    if codex_path:
        return [codex_path]
    npx_path = _which(NPX_BIN)
    if not npx_path:
        raise FileNotFoundError('Neither codex nor npx is on PATH')
    return [npx_path, '--yes', CODEX_NPM_PACKAGE]


def _execute_fallback_turn(
    base_cmd: list[str],
    config: CodexRunConfig,
    env: dict[str, str],
    effective_cwd: str,
    tdp: Path,
) -> dict[str, Any] | None:
    out_path_fallback = tdp / 'out-fallback.txt'
    fallback_prompt = (
        config.prompt
        + "\n\nReturn ONLY a valid JSON object matching the required schema. "
          "Do not include markdown, prose, or code fences."
    )
    fallback_cmd = [
        *base_cmd,
        CMD_EXEC,
        CMD_EPHEMERAL,
        CMD_SKIP_GIT,
        CMD_CWD,
        effective_cwd,
        CMD_MODEL,
        config.model,
        CMD_SANDBOX,
        SANDBOX_MODE,
        CMD_OUTPUT_LAST_MSG,
        str(out_path_fallback),
        '-',
    ]

    logger.warning("Codex exec fallback", extra={"structured_data": {"event": EVENT_EXEC_FALLBACK, "model": config.model, "reason": "schema parsing failure"}})

    proc_fb = subprocess.run(
        fallback_cmd,
        input=fallback_prompt,
        text=True,
        capture_output=True,
        timeout=config.timeout,
        env=env,
        cwd=effective_cwd or None,
    )
    if out_path_fallback.exists():
        raw_fb = out_path_fallback.read_text(encoding='utf-8', errors='replace').strip()
        parsed = _extract_json_object(raw_fb)
        if parsed is not None:
            return parsed
    parsed_stdout = _extract_json_object(proc_fb.stdout or '')
    if parsed_stdout is not None:
        return parsed_stdout
    parsed_stderr = _extract_json_object(proc_fb.stderr or '')
    if parsed_stderr is not None:
        return parsed_stderr
        
    return None


def run_turn_json(
    prompt: str,
    *,
    model: str,
    cwd: str,
    output_schema: dict[str, Any],
    timeout: int = 300,
) -> dict[str, Any]:
    # Use Pydantic to validate and structure the inputs
    config = CodexRunConfig(
        prompt=prompt,
        model=model,
        cwd=cwd,
        output_schema=output_schema,
        timeout=timeout,
    )
    
    base_cmd = _build_base_cmd()
    effective_cwd = config.cwd or str(Path.cwd())
    
    env = os.environ.copy()
    env.setdefault('PYTHONUNBUFFERED', '1')

    with tempfile.TemporaryDirectory(prefix='flume-codex-worker-') as td:
        tdp = Path(td)
        schema_path = tdp / 'schema.json'
        out_path = tdp / 'out.json'
        schema_path.write_text(json.dumps(config.output_schema, ensure_ascii=False), encoding='utf-8')

        cmd = [
            *base_cmd,
            CMD_EXEC,
            CMD_EPHEMERAL,
            CMD_SKIP_GIT,
            CMD_CWD,
            effective_cwd,
            CMD_MODEL,
            config.model,
            CMD_SANDBOX,
            SANDBOX_MODE,
            CMD_OUTPUT_SCHEMA,
            str(schema_path),
            CMD_OUTPUT_LAST_MSG,
            str(out_path),
            '-',
        ]

        logger.info("Codex exec start", extra={"structured_data": {"event": EVENT_EXEC_START, "model": config.model, "cwd": effective_cwd}})

        proc = subprocess.run(
            cmd,
            input=config.prompt,
            text=True,
            capture_output=True,
            timeout=config.timeout,
            env=env,
            cwd=effective_cwd or None,
        )

        if out_path.exists():
            raw = out_path.read_text(encoding='utf-8', errors='replace').strip()
            if raw:
                try:
                    return json.loads(raw)
                except Exception:
                    pass

        fallback_result = _execute_fallback_turn(base_cmd, config, env, effective_cwd, tdp)
        if fallback_result is not None:
            return fallback_result

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or '').strip()
            logger.error("Codex exec failed", extra={"structured_data": {"event": EVENT_EXEC_ERROR, "exit_code": proc.returncode, "error": err[-900:]}})
            raise RuntimeError(f'codex exec failed (exit {proc.returncode}): {err[-900:]}')
        if not out_path.exists():
            err = (proc.stderr or proc.stdout or '').strip()
            logger.error("Codex exec no output file", extra={"structured_data": {"event": EVENT_EXEC_NO_OUT, "error": err[:500]}})
            raise RuntimeError(f'codex exec produced no output file. {err[:500]}')
        raw = out_path.read_text(encoding='utf-8', errors='replace').strip()
        if not raw:
            err = (proc.stderr or proc.stdout or '').strip()
            logger.error("Codex exec empty output", extra={"structured_data": {"event": EVENT_EXEC_EMPTY_OUT, "error": err[:500]}})
            raise RuntimeError(f'codex exec returned empty output. {err[:500]}')
        return json.loads(raw)
