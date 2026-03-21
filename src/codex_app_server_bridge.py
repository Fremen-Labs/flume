from __future__ import annotations

import json
import os
import shutil
import subprocess
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
    return (Path.home() / '.codex' / 'auth.json').is_file()


def codex_available() -> bool:
    return bool(_which((os.environ.get('FLUME_CODEX_BIN') or 'codex').strip() or 'codex') or _which('npx'))


def _send(stdin, obj: dict[str, Any]) -> None:
    stdin.write(json.dumps(obj) + '\n')
    stdin.flush()


def _read_message(stdout) -> dict[str, Any]:
    line = stdout.readline()
    if not line:
        raise RuntimeError('Codex app-server closed the pipe unexpectedly')
    return json.loads(line)


def _extract_text(msg: dict[str, Any]) -> str:
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
            chunks: list[str] = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get('text'), str):
                    chunks.append(part['text'])
            if chunks:
                return ''.join(chunks)
    return ''


def run_turn_json(
    prompt: str,
    *,
    model: str,
    cwd: str,
    output_schema: dict[str, Any],
    timeout: int = 300,
) -> dict[str, Any]:
    cmd = launch_args(['--session-source', 'vscode'])
    env = os.environ.copy()
    env.setdefault('PYTHONUNBUFFERED', '1')
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=cwd,
        env=env,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    req_id = 1
    text_buf: list[str] = []
    try:
        _send(
            proc.stdin,
            {
                'method': 'initialize',
                'id': req_id,
                'params': {
                    'clientInfo': {
                        'name': 'flume_workers',
                        'title': 'Flume Workers',
                        'version': '0.1.0',
                    },
                    'capabilities': {
                        'optOutNotificationMethods': ['thread/started', 'item/started', 'item/completed']
                    },
                },
            },
        )
        _send(proc.stdin, {'method': 'initialized', 'params': {}})
        while True:
            msg = _read_message(proc.stdout)
            if msg.get('id') == req_id:
                if msg.get('error'):
                    raise RuntimeError(str(msg['error']))
                break
        req_id += 1
        _send(
            proc.stdin,
            {
                'method': 'thread/start',
                'id': req_id,
                'params': {
                    'ephemeral': True,
                    'model': model,
                    'cwd': cwd,
                    'approvalPolicy': 'never',
                    'sandboxPolicy': {
                        'type': 'workspaceWrite',
                        'writableRoots': [cwd],
                        'networkAccess': True,
                    },
                    'personality': 'pragmatic',
                    'serviceName': 'flume_workers',
                },
            },
        )
        thread_id = None
        while True:
            msg = _read_message(proc.stdout)
            if msg.get('id') == req_id:
                if msg.get('error'):
                    raise RuntimeError(str(msg['error']))
                thread = (msg.get('result') or {}).get('thread') or {}
                thread_id = thread.get('id')
                break
        if not thread_id:
            raise RuntimeError('No thread id returned from Codex app-server')
        req_id += 1
        _send(
            proc.stdin,
            {
                'method': 'turn/start',
                'id': req_id,
                'params': {
                    'threadId': thread_id,
                    'input': [{'type': 'text', 'text': prompt}],
                    'cwd': cwd,
                    'approvalPolicy': 'never',
                    'sandboxPolicy': {
                        'type': 'workspaceWrite',
                        'writableRoots': [cwd],
                        'networkAccess': True,
                    },
                    'model': model,
                    'personality': 'pragmatic',
                    'summary': 'concise',
                    'outputSchema': output_schema,
                },
            },
        )
        while True:
            msg = _read_message(proc.stdout)
            if msg.get('id') == req_id and msg.get('error'):
                raise RuntimeError(str(msg['error']))
            method = msg.get('method')
            if method == 'item/agentMessage/delta':
                delta = _extract_text(msg)
                if delta:
                    text_buf.append(delta)
            elif method == 'turn/completed':
                params = msg.get('params') or {}
                turn = params.get('turn') or {}
                if isinstance(turn, dict) and turn.get('error'):
                    raise RuntimeError(str(turn.get('error')))
                break
        raw = ''.join(text_buf).strip()
        if not raw:
            raise RuntimeError('Codex app-server returned no assistant output')
        return json.loads(raw)
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
