from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import base64
from pathlib import Path
from typing import Any

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
    auth_path = Path.home() / '.codex' / 'auth.json'
    if auth_path.is_file():
        try:
            doc = json.loads(auth_path.read_text(encoding='utf-8'))
            id_token = str((doc.get('tokens') or {}).get('id_token') or '').strip()
            if _jwt_like(id_token):
                return True
        except Exception:
            pass
        # Existing auth cache is malformed (or unreadable) — repair from env.
        return _ensure_codex_auth_from_env(auth_path)
    return _ensure_codex_auth_from_env(auth_path)


def codex_available() -> bool:
    return bool(_which((os.environ.get('FLUME_CODEX_BIN') or 'codex').strip() or 'codex') or _which('npx'))


def _jwt_like(token: str) -> bool:
    t = (token or '').strip()
    parts = t.split('.')
    if len(parts) != 3 or any(not p for p in parts):
        return False
    return True


def _ensure_codex_auth_from_env(auth_path: Path) -> bool:
    """Best-effort bridge from Flume OAuth env into Codex CLI auth cache."""
    raw = (os.environ.get('OPENAI_OAUTH_STATE_JSON') or '').strip()
    if not raw:
        return False
    try:
        state = json.loads(raw)
    except Exception:
        return False

    access = str(state.get('access') or state.get('access_token') or '').strip()
    refresh = str(state.get('refresh') or state.get('refresh_token') or '').strip()
    if not access:
        return False
    now_s = int(time.time())

    def _b64url_nopad(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")

    header = {"alg": "none", "typ": "JWT"}
    claims = {
        "email": str(state.get("email") or "flume@local"),
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": str(state.get("chatgpt_plan_type") or "pro"),
        },
        "iat": now_s,
        "exp": now_s + 3600,
    }
    id_token = (
        f"{_b64url_nopad(json.dumps(header, separators=(',', ':')).encode('utf-8'))}."
        f"{_b64url_nopad(json.dumps(claims, separators=(',', ':')).encode('utf-8'))}."
        f"{_b64url_nopad(b'sig')}"
    )
    auth_doc = {
        "auth_mode": "chatgpt",
        "openai_api_key": None,
        "tokens": {
            "id_token": id_token,
            "access_token": access,
            "refresh_token": refresh,
            "account_id": None,
        },
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_s)),
    }
    try:
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_text(json.dumps(auth_doc, indent=2), encoding='utf-8')
        return auth_path.is_file()
    except Exception:
        return False


def run_turn_json(
    prompt: str,
    *,
    model: str,
    cwd: str,
    output_schema: dict[str, Any],
    timeout: int = 300,
) -> dict[str, Any]:
    import json

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

        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
            cwd=effective_cwd or None,
        )

        # Some Codex CLI builds can emit a non-zero exit code even when
        # --output-last-message was successfully written. Prefer the structured
        # output file if present and parseable.
        if out_path.exists():
            raw = out_path.read_text(encoding='utf-8', errors='replace').strip()
            if raw:
                try:
                    return json.loads(raw)
                except Exception:
                    pass

        # Fallback path: retry without schema enforcement and parse JSON from
        # the last assistant message. This avoids exception-cap loops when
        # Codex exits non-zero for schema formatting reasons.
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
            raise RuntimeError(f'codex exec failed (exit {proc.returncode}): {err[-900:]}')
        if not out_path.exists():
            err = (proc.stderr or proc.stdout or '').strip()
            raise RuntimeError(f'codex exec produced no output file. {err[:500]}')
        raw = out_path.read_text(encoding='utf-8', errors='replace').strip()
        if not raw:
            err = (proc.stderr or proc.stdout or '').strip()
            raise RuntimeError(f'codex exec returned empty output. {err[:500]}')
        return json.loads(raw)
