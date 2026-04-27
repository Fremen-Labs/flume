from __future__ import annotations

import json
import urllib.error
import os
import subprocess
from pathlib import Path
from typing import Any

import codex_app_server


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
    cmd = codex_app_server.launch_args(['--session-source', 'vscode'])
    env = os.environ.copy()
    env.setdefault('PYTHONUNBUFFERED', '1')
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
        cwd=cwd or None,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    try:
        req_id = 1
        _send(
            proc.stdin,
            {
                'method': 'initialize',
                'id': req_id,
                'params': {
                    'clientInfo': {
                        'name': 'flume_dashboard_planner',
                        'title': 'Flume Dashboard Planner',
                        'version': '0.1.0',
                    },
                    'capabilities': {
                        'optOutNotificationMethods': [
                            'thread/started',
                            'item/started',
                            'item/completed',
                        ]
                    },
                },
            },
        )
        _send(proc.stdin, {'method': 'initialized', 'params': {}})
        req_id += 1
        _send(
            proc.stdin,
            {
                'method': 'thread/start',
                'id': req_id,
                'params': {
                    'ephemeral': True,
                    'model': model,
                    'cwd': cwd or str(Path.cwd()),
                    'approvalPolicy': 'never',
                    'sandboxPolicy': {
                        'type': 'workspaceWrite',
                        'writableRoots': [cwd or str(Path.cwd())],
                        'networkAccess': True,
                    },
                    'personality': 'pragmatic',
                    'serviceName': 'flume_dashboard_planner',
                },
            },
        )

        thread_id = None
        text_buf: list[str] = []
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError('Codex app-server exited before thread/start completed')
            msg = json.loads(line)
            if msg.get('id') == req_id:
                thread = (msg.get('result') or {}).get('thread') or {}
                thread_id = thread.get('id')
                break
            if msg.get('error'):
                raise RuntimeError(str(msg['error']))
        if not thread_id:
            raise RuntimeError('Codex app-server did not return a thread id')

        req_id += 1
        _send(
            proc.stdin,
            {
                'method': 'turn/start',
                'id': req_id,
                'params': {
                    'threadId': thread_id,
                    'input': [{'type': 'text', 'text': _messages_to_prompt(messages)}],
                    'cwd': cwd or str(Path.cwd()),
                    'approvalPolicy': 'never',
                    'sandboxPolicy': {
                        'type': 'workspaceWrite',
                        'writableRoots': [cwd or str(Path.cwd())],
                        'networkAccess': True,
                    },
                    'model': model,
                    'personality': 'pragmatic',
                    'summary': 'concise',
                    'outputSchema': _planner_output_schema(),
                },
            },
        )

        while True:
            line = proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            if msg.get('id') == req_id and msg.get('error'):
                raise RuntimeError(str(msg['error']))
            method = msg.get('method')
            if method == 'item/agentMessage/delta':
                delta = _extract_text_from_notification(msg)
                if delta:
                    text_buf.append(delta)
            elif method == 'turn/completed':
                params = msg.get('params') or {}
                turn = params.get('turn') or {}
                if isinstance(turn, dict) and turn.get('error'):
                    raise RuntimeError(str(turn.get('error')))
                break

        out = ''.join(text_buf).strip()
        if not out:
            raise RuntimeError('Codex app-server returned no planner output')
        return out
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError):
            pass
        try:
            proc.terminate()
        except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError):
            pass
        try:
            proc.wait(timeout=3)
        except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError):
            try:
                proc.kill()
            except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError):
                pass
