from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import codex_app_server

try:
    import websockets  # type: ignore[import-untyped]
except ImportError as e:  # pragma: no cover
    websockets = None  # type: ignore[assignment]
    _WS_IMPORT_ERROR = str(e)
else:
    _WS_IMPORT_ERROR = None


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


async def _planner_chat_async(messages: list[dict[str, Any]], model: str, cwd: str | None = None, timeout: int = 180) -> str:
    if websockets is None:
        raise RuntimeError(f"Python package 'websockets' is required for planner Codex routing: {_WS_IMPORT_ERROR}")
    codex_app_server.start_background_if_needed(Path(cwd or str(Path.cwd())))
    uri = codex_app_server.codex_listen_url()
    text_buf: list[str] = []
    last_err = None
    for _ in range(20):
        try:
            async with websockets.connect(uri, max_size=None, ping_interval=20, ping_timeout=120) as ws:
                break
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.5)
    else:
        raise RuntimeError(str(last_err) if last_err else f'Could not connect to {uri}')

    async with websockets.connect(uri, max_size=None, ping_interval=20, ping_timeout=120) as ws:
        req_id = 1
        await ws.send(json.dumps({
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
        }))
        await ws.send(json.dumps({'method': 'initialized', 'params': {}}))
        req_id += 1
        await ws.send(json.dumps({
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
        }))
        thread_id = None
        while True:
            line = await asyncio.wait_for(ws.recv(), timeout=timeout)
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
        await ws.send(json.dumps({
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
        }))
        while True:
            line = await asyncio.wait_for(ws.recv(), timeout=timeout)
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


def planner_chat(messages: list[dict[str, Any]], model: str, cwd: str | None = None, timeout: int = 180) -> str:
    return asyncio.run(_planner_chat_async(messages, model=model, cwd=cwd, timeout=timeout))
