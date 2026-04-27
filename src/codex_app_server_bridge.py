from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_LISTEN_URL = 'stdio://'
DEFAULT_MODEL = 'gpt-5-codex'


def _which(name: str) -> str | None:
    p = shutil.which(name)
    return p if p else None


def launch_args(extra_args: list[str] | None = None) -> list[str]:
    extra = list(extra_args or [])
    codex_bin = (os.environ.get('FLUME_CODEX_BIN') or 'codex').strip() or 'codex'
    codex_path = _which(codex_bin)
    if codex_path:
        return [codex_path, 'app-server', '--listen', DEFAULT_LISTEN_URL, *extra]
    npx_path = _which('npx')
    if npx_path:
        return [npx_path, '--yes', '@openai/codex', 'app-server', '--listen', DEFAULT_LISTEN_URL, *extra]
    raise FileNotFoundError('Neither codex nor npx is on PATH')


def codex_auth_present() -> bool:
    return (Path.home() / '.codex' / 'auth.json').is_file()


def codex_available() -> bool:
    return bool(_which((os.environ.get('FLUME_CODEX_BIN') or 'codex').strip() or 'codex') or _which('npx'))


def run_turn_json(
    prompt: str,
    *,
    model: str,
    cwd: str,
    output_schema: dict[str, Any],
    timeout: int = 300,
) -> dict[str, Any]:
    codex_bin = (os.environ.get('FLUME_CODEX_BIN') or 'codex').strip() or 'codex'
    codex_path = _which(codex_bin)
    if codex_path:
        base_cmd = [codex_path]
    else:
        npx_path = _which('npx')
        if not npx_path:
            raise FileNotFoundError('Neither codex nor npx is on PATH')
        base_cmd = [npx_path, '--yes', '@openai/codex']

    effective_cwd = cwd or str(Path.cwd())
    env = os.environ.copy()
    env.setdefault('PYTHONUNBUFFERED', '1')

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

    with tempfile.TemporaryDirectory(prefix='flume-codex-worker-') as td:
        tdp = Path(td)
        schema_path = tdp / 'schema.json'
        out_path = tdp / 'out.json'
        schema_path.write_text(json.dumps(output_schema, ensure_ascii=False), encoding='utf-8')

        cmd = [
            *base_cmd,
            'exec',
            '--ephemeral',
            '--skip-git-repo-check',
            '-C',
            effective_cwd,
            '-m',
            model,
            '--sandbox',
            'workspace-write',
            '--output-schema',
            str(schema_path),
            '--output-last-message',
            str(out_path),
            '-',
        ]

        logger.info(json.dumps({"event": "codex_exec_start", "model": model, "cwd": effective_cwd}))

        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
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

        out_path_fallback = tdp / 'out-fallback.txt'
        fallback_prompt = (
            prompt
            + "\n\nReturn ONLY a valid JSON object matching the required schema. "
              "Do not include markdown, prose, or code fences."
        )
        fallback_cmd = [
            *base_cmd,
            'exec',
            '--ephemeral',
            '--skip-git-repo-check',
            '-C',
            effective_cwd,
            '-m',
            model,
            '--sandbox',
            'workspace-write',
            '--output-last-message',
            str(out_path_fallback),
            '-',
        ]
        
        logger.warning(json.dumps({"event": "codex_exec_fallback", "model": model, "reason": "schema parsing failure"}))
        
        proc_fb = subprocess.run(
            fallback_cmd,
            input=fallback_prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
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

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or '').strip()
            logger.error(json.dumps({"event": "codex_exec_error", "exit_code": proc.returncode, "error": err[-900:]}))
            raise RuntimeError(f'codex exec failed (exit {proc.returncode}): {err[-900:]}')
        if not out_path.exists():
            err = (proc.stderr or proc.stdout or '').strip()
            logger.error(json.dumps({"event": "codex_exec_no_output_file", "error": err[:500]}))
            raise RuntimeError(f'codex exec produced no output file. {err[:500]}')
        raw = out_path.read_text(encoding='utf-8', errors='replace').strip()
        if not raw:
            err = (proc.stderr or proc.stdout or '').strip()
            logger.error(json.dumps({"event": "codex_exec_empty_output", "error": err[:500]}))
            raise RuntimeError(f'codex exec returned empty output. {err[:500]}')
        return json.loads(raw)
