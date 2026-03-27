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
  gemini           : Uses Gemini's OpenAI-compatible endpoint. Set LLM_API_KEY (AI Studio key);
                     uses Authorization: Bearer <key> per Google docs.
"""

import json
import os
import urllib.request
import urllib.error

def _provider() -> str:
    return os.environ.get('LLM_PROVIDER', 'ollama').lower()


def _base_url_env() -> str:
    return os.environ.get('LLM_BASE_URL', 'http://localhost:11434').rstrip('/')


def _api_key() -> str:
    return os.environ.get('LLM_API_KEY', '')


def _default_model() -> str:
    return os.environ.get('LLM_MODEL', 'llama3.2')

_PROVIDER_BASE_URLS = {
    'openai': 'https://api.openai.com',
    'anthropic': 'https://api.anthropic.com',
    'gemini': 'https://generativelanguage.googleapis.com/v1beta/openai',
}

# Retired IDs on generativelanguage OpenAI-compat API → current stable names
_GEMINI_MODEL_ALIASES = {
    'gemini-1.5-flash': 'gemini-2.5-flash',
    'gemini-1.5-flash-latest': 'gemini-2.5-flash',
    'gemini-1.5-flash-8b': 'gemini-2.5-flash',
    'gemini-1.5-pro': 'gemini-2.5-pro',
    'gemini-1.5-pro-latest': 'gemini-2.5-pro',
    'gemini-2.0-flash': 'gemini-2.5-flash',
    'gemini-2.0-flash-lite': 'gemini-2.5-flash-lite',
}


def _normalize_gemini_model(model_id: str) -> str:
    m = (model_id or '').strip() or 'gemini-2.5-flash'
    return _GEMINI_MODEL_ALIASES.get(m, m)


def _base_url(provider=None, base_url_override=None):
    if base_url_override:
        return base_url_override.rstrip('/')
    p = (provider or _provider()).lower()
    if p in _PROVIDER_BASE_URLS and not os.environ.get('LLM_BASE_URL'):
        return _PROVIDER_BASE_URLS[p]
    return _base_url_env()


import time

def _post(url, payload, extra_headers=None, timeout=120, max_retries=4):
    headers = {'Content-Type': 'application/json'}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method='POST',
    )
    
    last_err = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in [429, 500, 502, 503, 504]:
                time.sleep(2 ** attempt)
                continue
            raise e
        except urllib.error.URLError as e:
            last_err = e
            time.sleep(2 ** attempt)
            continue
            
    if last_err:
        raise last_err


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def _ollama_chat(messages, model, temperature, max_tokens, base_url_override=None, timeout=120):
    data = _post(
        f'{_base_url(base_url_override=base_url_override)}/api/chat',
        {
            'model': model,
            'messages': messages,
            'stream': False,
            'options': {'temperature': temperature, 'num_predict': max_tokens},
        },
        timeout=timeout,
    )
    return data.get('message', {}).get('content', '')


def _ollama_chat_tools(messages, tools, model, temperature, max_tokens, base_url_override=None):
    return _post(
        f'{_base_url(base_url_override=base_url_override)}/api/chat',
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
    key = (_api_key() or '').strip()
    if not key:
        raise RuntimeError('LLM_API_KEY is empty.')
    return {'Authorization': f'Bearer {key}'}


def _openai_chat(messages, model, temperature, max_tokens, provider=None, base_url_override=None, timeout=120):
    url = _base_url(provider, base_url_override) + '/v1/chat/completions'
    data = _post(
        url,
        {'model': model, 'messages': messages, 'temperature': temperature, 'max_tokens': max_tokens},
        _openai_headers(),
        timeout=timeout,
    )
    return (data['choices'][0]['message'].get('content') or '').strip()


def _openai_chat_tools(messages, tools, model, temperature, max_tokens, provider=None, base_url_override=None):
    url = _base_url(provider, base_url_override) + '/v1/chat/completions'
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
        'x-api-key': _api_key(),
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

def chat(messages, model=None, *, temperature=0.3, max_tokens=8192, provider_override=None, base_url_override=None, timeout_seconds=120):
    """Call the configured LLM and return the assistant's text response.

    Args:
        messages:    List of {role, content} dicts in OpenAI format.
        model:       Model name override; falls back to LLM_MODEL env var.
        temperature: Sampling temperature (0.0–1.0).
        max_tokens:  Maximum tokens to generate.

    Returns:
        str: The assistant's text response.
    """
    p = (provider_override or _provider()).lower()
    m = model or _default_model()
    if p == 'gemini':
        m = _normalize_gemini_model(m)
    if p == 'ollama':
        return _ollama_chat(messages, m, temperature, max_tokens, base_url_override, timeout=timeout_seconds)
    elif p == 'anthropic':
        return _anthropic_chat(messages, m, temperature, max_tokens)
    else:
        return _openai_chat(messages, m, temperature, max_tokens, provider=p, base_url_override=base_url_override, timeout=timeout_seconds)


def chat_with_tools(messages, tools, model=None, *, temperature=0.2, max_tokens=4096, provider_override=None, base_url_override=None):
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
    p = (provider_override or _provider()).lower()
    m = model or _default_model()
    if p == 'gemini':
        m = _normalize_gemini_model(m)
    if p == 'ollama':
        return _ollama_chat_tools(messages, tools, m, temperature, max_tokens, base_url_override)
    elif p == 'anthropic':
        return _anthropic_chat_tools(messages, tools, m, temperature, max_tokens)
    else:
        return _openai_chat_tools(messages, tools, m, temperature, max_tokens, provider=p, base_url_override=base_url_override)
