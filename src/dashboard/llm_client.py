#!/usr/bin/env python3
"""Flume LLM Client — stdlib only, zero third-party dependencies.

Provides a unified interface for multiple LLM providers. Configure via
environment variables:

  LLM_PROVIDER   : ollama | openai | openai_compatible | anthropic | gemini
                   Default: ollama
  LLM_BASE_URL   : Base URL for 'ollama' (default: http://localhost:11434)
                   or full base URL for 'openai_compatible'
  LLM_API_KEY    : API key for openai, anthropic, gemini, openai_compatible
  LLM_MODEL      : Default model name (default: llama3.2)

Public API:
  chat(messages, model=None, *, temperature=0.3, max_tokens=8192) -> str
      Call the LLM and return the assistant text response.

  chat_with_tools(messages, tools, model=None, *, temperature=0.2, max_tokens=4096) -> dict
      Call the LLM with tool definitions. Returns an Ollama-compatible dict:
        {'message': {'role': 'assistant', 'content': str, 'tool_calls': [...]}}
      where each tool_call is: {'function': {'name': str, 'arguments': dict}}

Provider-specific notes:
  ollama           : Requires local Ollama at LLM_BASE_URL. Tools use /api/chat.
  openai           : Uses api.openai.com/v1/chat/completions. Set LLM_API_KEY.
  openai_compatible: Uses LLM_BASE_URL/v1/chat/completions. Set LLM_API_KEY.
                     Covers Groq, Together, Mistral, Azure OpenAI, and more.
  anthropic        : Uses api.anthropic.com/v1/messages. Set LLM_API_KEY.
  gemini           : Uses Gemini's OpenAI-compatible endpoint. Set LLM_API_KEY.
"""

import json
import os
import time
from pathlib import Path
import urllib.request
import urllib.error

_PROVIDER = os.environ.get('LLM_PROVIDER', 'ollama').lower()
_BASE_URL = os.environ.get('LLM_BASE_URL', 'http://localhost:11434').rstrip('/')
_API_KEY = os.environ.get('LLM_API_KEY', '')
_DEFAULT_MODEL = os.environ.get('LLM_MODEL', 'llama3.2')
_OPENAI_OAUTH_STATE_FILE = os.environ.get('OPENAI_OAUTH_STATE_FILE', '').strip()
_OPENAI_OAUTH_TOKEN_URL = os.environ.get('OPENAI_OAUTH_TOKEN_URL', 'https://auth.openai.com/oauth/token').strip()

_PROVIDER_BASE_URLS = {
    'openai': 'https://api.openai.com',
    'anthropic': 'https://api.anthropic.com',
    'gemini': 'https://generativelanguage.googleapis.com/v1beta/openai',
}


def _base_url():
    if _PROVIDER in _PROVIDER_BASE_URLS and not os.environ.get('LLM_BASE_URL'):
        return _PROVIDER_BASE_URLS[_PROVIDER]
    return _BASE_URL


def _post(url, payload, extra_headers=None, timeout=120):
    headers = {'Content-Type': 'application/json'}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _oauth_state_path():
    if not _OPENAI_OAUTH_STATE_FILE:
        return None
    p = Path(_OPENAI_OAUTH_STATE_FILE)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


def _refresh_oauth_access_token() -> str:
    state_path = _oauth_state_path()
    if not state_path or not state_path.exists():
        return ''
    state = _load_json(state_path)
    refresh_token = str(state.get('refresh') or '').strip()
    client_id = str(state.get('client_id') or '').strip()
    if not refresh_token:
        return ''
    now_ms = int(time.time() * 1000)
    access = str(state.get('access') or '').strip()
    expires = int(state.get('expires') or 0)
    if access and expires and expires > now_ms + 60_000:
        return access
    if not client_id:
        return ''

    payload = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': client_id,
    }
    data = _post(_OPENAI_OAUTH_TOKEN_URL, payload, timeout=30)
    new_access = str(data.get('access_token') or '').strip()
    if not new_access:
        return ''

    state['access'] = new_access
    if data.get('refresh_token'):
        state['refresh'] = data['refresh_token']
    expires_in = int(data.get('expires_in') or 0)
    if expires_in > 0:
        state['expires'] = now_ms + (expires_in * 1000)
    _save_json(state_path, state)
    return new_access


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def _ollama_chat(messages, model, temperature, max_tokens):
    data = _post(
        f'{_BASE_URL}/api/chat',
        {
            'model': model,
            'messages': messages,
            'stream': False,
            'options': {'temperature': temperature, 'num_predict': max_tokens},
        },
    )
    return data.get('message', {}).get('content', '')


def _ollama_chat_tools(messages, tools, model, temperature, max_tokens):
    return _post(
        f'{_BASE_URL}/api/chat',
        {
            'model': model,
            'messages': messages,
            'tools': tools,
            'stream': False,
            'options': {'temperature': temperature, 'num_predict': max_tokens},
        },
        timeout=180,
    )


# ---------------------------------------------------------------------------
# OpenAI / OpenAI-compatible / Gemini
# ---------------------------------------------------------------------------

def _openai_headers():
    key = _API_KEY or _refresh_oauth_access_token()
    if not key:
        raise RuntimeError(
            'LLM_API_KEY is empty and OpenAI OAuth token refresh is not configured. '
            'Set LLM_API_KEY or configure OPENAI_OAUTH_STATE_FILE.'
        )
    return {'Authorization': f'Bearer {key}'}


def _openai_chat(messages, model, temperature, max_tokens):
    url = _base_url() + '/v1/chat/completions'
    data = _post(
        url,
        {'model': model, 'messages': messages, 'temperature': temperature, 'max_tokens': max_tokens},
        _openai_headers(),
    )
    return (data['choices'][0]['message'].get('content') or '').strip()


def _openai_chat_tools(messages, tools, model, temperature, max_tokens):
    url = _base_url() + '/v1/chat/completions'
    data = _post(
        url,
        {
            'model': model,
            'messages': messages,
            'tools': tools,
            'temperature': temperature,
            'max_tokens': max_tokens,
        },
        _openai_headers(),
        timeout=180,
    )
    choice_msg = data['choices'][0]['message']
    tool_calls = []
    for tc in (choice_msg.get('tool_calls') or []):
        args = tc['function']['arguments']
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                pass
        tool_calls.append({'function': {'name': tc['function']['name'], 'arguments': args}})
    return {
        'message': {
            'role': 'assistant',
            'content': choice_msg.get('content') or '',
            'tool_calls': tool_calls,
        }
    }


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

def _anthropic_headers():
    return {
        'x-api-key': _API_KEY,
        'anthropic-version': '2023-06-01',
    }


def _split_system(messages):
    system = ''
    rest = []
    for m in messages:
        if m.get('role') == 'system':
            system = m['content']
        else:
            rest.append(m)
    return system, rest


def _anthropic_chat(messages, model, temperature, max_tokens):
    system, rest = _split_system(messages)
    payload = {
        'model': model,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'messages': rest,
    }
    if system:
        payload['system'] = system
    data = _post('https://api.anthropic.com/v1/messages', payload, _anthropic_headers())
    for block in data.get('content', []):
        if block.get('type') == 'text':
            return block['text']
    return ''


def _openai_tools_to_anthropic(tools):
    """Convert OpenAI tool-call format to Anthropic's tool format."""
    out = []
    for t in tools:
        fn = t.get('function', {})
        out.append({
            'name': fn['name'],
            'description': fn.get('description', ''),
            'input_schema': fn.get('parameters', {'type': 'object', 'properties': {}}),
        })
    return out


def _anthropic_chat_tools(messages, tools, model, temperature, max_tokens):
    system, rest = _split_system(messages)
    payload = {
        'model': model,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'messages': rest,
        'tools': _openai_tools_to_anthropic(tools),
    }
    if system:
        payload['system'] = system
    data = _post('https://api.anthropic.com/v1/messages', payload, _anthropic_headers(), timeout=180)
    content_text = ''
    tool_calls = []
    for block in data.get('content', []):
        if block.get('type') == 'text':
            content_text = block['text']
        elif block.get('type') == 'tool_use':
            tool_calls.append({
                'function': {
                    'name': block['name'],
                    'arguments': block.get('input', {}),
                }
            })
    return {
        'message': {
            'role': 'assistant',
            'content': content_text,
            'tool_calls': tool_calls,
        }
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chat(messages, model=None, *, temperature=0.3, max_tokens=8192):
    """Call the configured LLM and return the assistant's text response.

    Args:
        messages:    List of {role, content} dicts in OpenAI format.
        model:       Model name override; falls back to LLM_MODEL env var.
        temperature: Sampling temperature (0.0–1.0).
        max_tokens:  Maximum tokens to generate.

    Returns:
        str: The assistant's text response.
    """
    m = model or _DEFAULT_MODEL
    if _PROVIDER == 'ollama':
        return _ollama_chat(messages, m, temperature, max_tokens)
    elif _PROVIDER == 'anthropic':
        return _anthropic_chat(messages, m, temperature, max_tokens)
    else:
        return _openai_chat(messages, m, temperature, max_tokens)


def chat_with_tools(messages, tools, model=None, *, temperature=0.2, max_tokens=4096):
    """Call the configured LLM with tool definitions.

    Args:
        messages:    List of {role, content} dicts in OpenAI format.
        tools:       List of tool definitions in OpenAI function-calling format.
        model:       Model name override; falls back to LLM_MODEL env var.
        temperature: Sampling temperature.
        max_tokens:  Maximum tokens to generate.

    Returns:
        dict: Ollama-compatible response dict:
            {
                'message': {
                    'role': 'assistant',
                    'content': str,
                    'tool_calls': [{'function': {'name': str, 'arguments': dict}}]
                }
            }
    """
    m = model or _DEFAULT_MODEL
    if _PROVIDER == 'ollama':
        return _ollama_chat_tools(messages, tools, m, temperature, max_tokens)
    elif _PROVIDER == 'anthropic':
        return _anthropic_chat_tools(messages, tools, m, temperature, max_tokens)
    else:
        return _openai_chat_tools(messages, tools, m, temperature, max_tokens)
