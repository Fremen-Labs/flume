#!/usr/bin/env python3
"""Flume LLM Client — stdlib only, zero third-party dependencies.

Configuration is read from **os.environ on each request** so `.env` updates apply without
restarting the process (e.g. after Settings save, `flume codex-oauth`, or editing the file).

  LLM_PROVIDER   : ollama | openai | openai_compatible | anthropic | gemini
  LLM_BASE_URL   : Base URL for ollama or openai_compatible
  LLM_API_KEY    : API key (or OAuth access token for OpenAI)
  LLM_MODEL      : Default model name
  OPENAI_OAUTH_STATE_FILE / OPENAI_OAUTH_TOKEN_URL : OpenAI ChatGPT OAuth refresh
  OPENAI_OAUTH_SCOPES       : Optional; space-separated scopes for refresh (defaults below)
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from openai_oauth_state import load_state_from_env_or_file, save_state_to_env_or_file

_PROVIDER_BASE_URLS = {
    'openai': 'https://api.openai.com',
    'anthropic': 'https://api.anthropic.com',
    'gemini': 'https://generativelanguage.googleapis.com/v1beta/openai',
    'xai': 'https://api.x.ai',
    'mistral': 'https://api.mistral.ai',
    'cohere': 'https://api.cohere.ai/v1',
}

_DEFAULT_OPENAI_OAUTH_SCOPES = (
    'openid profile email offline_access model.request api.model.read api.responses.write'
)

_UNSET = object()


def _openai_oauth_refresh_scopes() -> str | None:
    raw = os.environ.get('OPENAI_OAUTH_SCOPES')
    if raw is None:
        return _DEFAULT_OPENAI_OAUTH_SCOPES
    s = str(raw).strip()
    return s or None


def default_base_url_for_provider(provider_id: str) -> str:
    """Public base URL when switching provider for a single call (e.g. per-task override)."""
    pid = (provider_id or '').strip().lower()
    if pid == 'openai_compatible':
        return os.environ.get('LLM_BASE_URL', '').rstrip('/')
    if pid in _PROVIDER_BASE_URLS:
        return _PROVIDER_BASE_URLS[pid]
    if pid == 'ollama':
        return os.environ.get('LLM_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
    return os.environ.get('LLM_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')


def _merge_runtime(
    provider_override: str | None = None,
    base_url_override: str | None = None,
    api_key_override: object = _UNSET,
):
    rt = _runtime()
    if not provider_override:
        if api_key_override is not _UNSET:
            rt = {**rt, 'api_key': str(api_key_override or '').strip()}
        return rt
    prov = provider_override.strip().lower()
    rt = {**rt, 'provider': prov}
    if base_url_override is not None and str(base_url_override).strip():
        rt['base_url'] = str(base_url_override).strip().rstrip('/')
    else:
        rt['base_url'] = default_base_url_for_provider(prov)
    if api_key_override is not _UNSET:
        rt = {**rt, 'api_key': str(api_key_override or '').strip()}
    return rt


def _runtime():
    """Current LLM config from the environment (call on each public API use)."""
    return {
        'provider': os.environ.get('LLM_PROVIDER', 'ollama').lower(),
        'base_url': os.environ.get('LLM_BASE_URL', 'http://localhost:11434').rstrip('/'),
        'api_key': os.environ.get('LLM_API_KEY', ''),
        'default_model': os.environ.get('LLM_MODEL', 'llama3.2'),
        'oauth_state_file': os.environ.get('OPENAI_OAUTH_STATE_FILE', '').strip(),
        'oauth_token_url': os.environ.get(
            'OPENAI_OAUTH_TOKEN_URL', 'https://auth.openai.com/oauth/token'
        ).strip(),
    }


def _effective_base_url(rt: dict) -> str:
    prov = rt['provider']
    if prov in _PROVIDER_BASE_URLS and not (os.environ.get('LLM_BASE_URL') or '').strip():
        return _PROVIDER_BASE_URLS[prov]
    return rt['base_url']


def _looks_like_ollama_or_local_llm_base(url: str) -> bool:
    u = (url or '').strip().lower()
    if not u:
        return False
    return (
        ':11434' in u
        or 'localhost' in u
        or '127.0.0.1' in u
        or u.startswith('http://0.0.0.0:')
    )


def _openai_api_origin(rt: dict) -> str:
    """
    Host for OpenAI /v1/* HTTP APIs. Avoid posting OAuth to Ollama when LLM_BASE_URL is stale.
    """
    if rt.get('provider') != 'openai':
        return _effective_base_url(rt)
    explicit = (os.environ.get('LLM_BASE_URL') or '').strip().rstrip('/')
    if explicit and _looks_like_ollama_or_local_llm_base(explicit):
        return _PROVIDER_BASE_URLS['openai']
    if not explicit:
        return _PROVIDER_BASE_URLS['openai']
    return explicit


def _looks_like_openai_platform_api_key(key: str) -> bool:
    t = (key or '').strip()
    return t.startswith('sk-') or t.startswith('sk_')


def _jwt_scope_list(access_token: str) -> list[str] | None:
    """
    Decode JWT `scp` / `scope` / `roles` without verifying signature.
    None if not a classic 3-segment JWT or parse fails.
    """
    t = (access_token or '').strip()
    parts = t.split('.')
    if len(parts) != 3:
        return None
    try:
        payload_b64 = parts[1]
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
        scopes: list[str] = []
        scp_raw = payload.get('scp')
        if isinstance(scp_raw, str):
            scopes.extend(x for x in scp_raw.split() if x)
        elif isinstance(scp_raw, list):
            scopes.extend(str(x) for x in scp_raw if x)
        scope_one = payload.get('scope')
        if isinstance(scope_one, str):
            scopes.extend(x for x in scope_one.split() if x)
        roles = payload.get('roles')
        if isinstance(roles, list):
            scopes.extend(str(x) for x in roles if x)
        return scopes
    except Exception:
        return None


def _openai_bearer_uses_responses_api(rt: dict) -> bool:
    """
    Platform API keys use /v1/chat/completions.

    OAuth: /v1/responses only if JWT lists api.responses.write; otherwise chat/completions
    (Codex connector tokens, opaque tokens, JWE, etc.).
    """
    if rt['provider'] != 'openai':
        return False
    key = _openai_bearer_for_request(rt)
    if _looks_like_openai_platform_api_key(key):
        return False
    scopes = _jwt_scope_list(key)
    if scopes is None:
        return False
    return 'api.responses.write' in scopes


def _chat_messages_to_responses_input(messages: list) -> list:
    out = []
    for msg in messages:
        role = msg.get('role') or 'user'
        content = msg.get('content', '')
        api_role = 'developer' if role == 'system' else role
        if isinstance(content, list):
            out.append({'role': api_role, 'content': content})
        else:
            out.append(
                {
                    'role': api_role,
                    'content': [{'type': 'input_text', 'text': str(content)}],
                }
            )
    return out


def _responses_output_text(data: dict) -> str:
    if not isinstance(data, dict):
        return ''
    status = (data.get('status') or '').strip().lower()
    if status in ('failed', 'cancelled', 'incomplete'):
        err = data.get('error')
        msg = ''
        if isinstance(err, dict):
            msg = str(err.get('message') or err.get('code') or err)
        elif isinstance(err, str):
            msg = err
        if msg:
            raise RuntimeError(msg)
    top = data.get('output_text')
    if isinstance(top, str) and top.strip():
        return top.strip()
    parts: list[str] = []
    for item in data.get('output') or []:
        if item.get('type') != 'message':
            continue
        for block in item.get('content') or []:
            if block.get('type') == 'output_text':
                parts.append(str(block.get('text') or ''))
            elif isinstance(block.get('text'), str):
                parts.append(block['text'])
    out = ''.join(parts).strip()
    if not out and status == 'completed':
        raise RuntimeError('OpenAI Responses API returned no output text')
    return out


def _openai_responses_chat(messages, model, temperature, max_tokens, rt: dict) -> str:
    url = _openai_api_origin(rt).rstrip('/') + '/v1/responses'
    payload: dict = {
        'model': model,
        'input': _chat_messages_to_responses_input(messages),
        'temperature': temperature,
    }
    if max_tokens and max_tokens > 0:
        payload['max_output_tokens'] = max_tokens
    data = _post(url, payload, _openai_headers(rt), timeout=120)
    return _responses_output_text(data)


def _post_urlencoded(url: str, form: dict, timeout: int = 120) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode(errors='replace')[:2000]
        except Exception:
            pass
        msg = f'HTTP {e.code} {e.reason} calling {url}'
        if body:
            msg += f' — {body}'
        raise RuntimeError(msg) from e


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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode(errors='replace')[:2000]
        except Exception:
            pass
        msg = f'HTTP {e.code} {e.reason} calling {url}'
        if body:
            msg += f' — {body}'
        if e.code == 401 and 'openai.com' in (url or '').lower():
            if 'model.request' in body:
                msg += (
                    ' Hint: ChatGPT/Codex browser OAuth cannot obtain model.request; this endpoint requires it. '
                    'Use an OpenAI platform API key (sk-…) in Settings → LLM → API Key.'
                )
            elif 'Missing scopes' in body or 'api.responses.write' in body:
                msg += (
                    ' Hint: Codex OAuth JWTs usually omit api.responses.write; Flume uses /v1/chat/completions. '
                    'For persistent 401, use a platform sk- API key.'
                )
            else:
                msg += (
                    ' Hint: For ChatGPT/Codex OAuth use Refresh OAuth token; also ensure LLM_BASE_URL '
                    'is not still set to Ollama (localhost:11434) when LLM_PROVIDER=openai.'
                )
        raise RuntimeError(msg) from e


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _oauth_state_path(rt: dict):
    sf = rt['oauth_state_file']
    if not sf:
        return None
    loom = os.environ.get('LOOM_WORKSPACE', '').strip()
    if loom:
        try:
            from flume_secrets import resolve_oauth_state_path

            return resolve_oauth_state_path(Path(loom), sf)
        except ImportError:
            pass
    p = Path(sf)
    return p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()


def _refresh_oauth_access_token(rt: dict) -> str:
    state_path = _oauth_state_path(rt)
    if state_path is None and not (os.environ.get('OPENAI_OAUTH_STATE_JSON') or '').strip():
        return ''
    if state_path is not None and (not state_path.exists()) and not (os.environ.get('OPENAI_OAUTH_STATE_JSON') or '').strip():
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

    token_url = rt['oauth_token_url'] or 'https://auth.openai.com/oauth/token'
    form = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': client_id,
    }
    scp = _openai_oauth_refresh_scopes()
    if scp:
        form['scope'] = scp
    data = _post_urlencoded(token_url, form, timeout=30)
    new_access = str(data.get('access_token') or '').strip()
    if not new_access:
        return ''

    state['access'] = new_access
    if data.get('refresh_token'):
        state['refresh'] = data['refresh_token']
    expires_in = int(data.get('expires_in') or 0)
    if expires_in > 0:
        state['expires'] = now_ms + (expires_in * 1000)
    save_state_to_env_or_file(state, state_path)
    return new_access


def _record_telemetry(provider: str, model: str, input_tokens: int, output_tokens: int):
    try:
        worker_name = os.environ.get('FLUME_WORKER_NAME')
        if not worker_name or (not input_tokens and not output_tokens):
            return
        worker_role = os.environ.get('FLUME_WORKER_ROLE', 'unknown')
        import ssl
        from datetime import datetime, timezone
        es_url = os.environ.get('ES_URL', 'https://localhost:9200').rstrip('/')
        es_key = os.environ.get('ES_API_KEY', '')
        if not es_key or not es_url:
            return
        ctx = None
        if os.environ.get('ES_VERIFY_TLS', 'false').lower() != 'true':
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        doc = {
            'worker_name': worker_name,
            'worker_role': worker_role,
            'provider': provider,
            'model': model,
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'created_at': datetime.now(timezone.utc).isoformat()
        }
        req = urllib.request.Request(
            f"{es_url}/agent-token-telemetry/_doc",
            data=json.dumps(doc).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': f'ApiKey {es_key}'},
            method='POST'
        )
        with urllib.request.urlopen(req, context=ctx, timeout=3):
            pass
    except Exception:
        pass


def _ollama_chat(messages, model, temperature, max_tokens, rt: dict):
    base = rt['base_url']
    data = _post(
        f'{base}/api/chat',
        {
            'model': model,
            'messages': messages,
            'stream': False,
            'options': {'temperature': temperature, 'num_predict': max_tokens},
        },
    )
    _record_telemetry(rt['provider'], model, data.get('prompt_eval_count', 0), data.get('eval_count', 0))
    return data.get('message', {}).get('content', '')


def _ollama_chat_tools(messages, tools, model, temperature, max_tokens, rt: dict):
    base = rt['base_url']
    return _post(
        f'{base}/api/chat',
        {
            'model': model,
            'messages': messages,
            'tools': tools,
            'stream': False,
            'options': {'temperature': temperature, 'num_predict': max_tokens},
        },
        timeout=180,
    )
    _record_telemetry(rt['provider'], model, data.get('prompt_eval_count', 0), data.get('eval_count', 0))
    return data


def _openai_bearer_for_request(rt: dict) -> str:
    """
    Prefer OAuth state file over LLM_API_KEY for ChatGPT/Codex tokens — .env can hold a stale JWT
    after login-browser updates `.openai-oauth.json`.
    """
    api_key = (rt.get('api_key') or '').strip()
    oauth_file = (rt.get('oauth_state_file') or '').strip()
    if (
        rt['provider'] == 'openai'
        and oauth_file
        and not _looks_like_openai_platform_api_key(api_key)
    ):
        return _refresh_oauth_access_token(rt) or api_key
    return api_key or _refresh_oauth_access_token(rt)


def _openai_headers(rt: dict):
    key = _openai_bearer_for_request(rt)
    if not key:
        prov = (rt.get('provider') or '').strip().lower()
        if prov == 'openai' and (rt.get('oauth_state_file') or '').strip():
            raise RuntimeError(
                'LLM_API_KEY is empty and OpenAI OAuth token refresh did not yield a token. '
                'Set LLM_API_KEY in Settings, or configure OPENAI_OAUTH_STATE_FILE and refresh.'
            )
        raise RuntimeError(
            'LLM_API_KEY is empty. In Settings → LLM, paste your API key and Save, or click '
            '"Use" on an active saved key (worker-manager/llm_credentials.json). '
            'For OpenAI with OAuth only, configure OPENAI_OAUTH_STATE_FILE.'
        )
    # Gemini OpenAI-compat: Authorization: Bearer <GEMINI_API_KEY>
    # https://ai.google.dev/gemini-api/docs/openai
    return {'Authorization': f'Bearer {key}'}


def _openai_chat(messages, model, temperature, max_tokens, rt: dict):
    if _openai_bearer_uses_responses_api(rt):
        return _openai_responses_chat(messages, model, temperature, max_tokens, rt)
    url = _openai_api_origin(rt).rstrip('/') + '/v1/chat/completions'
    data = _post(
        url,
        {'model': model, 'messages': messages, 'temperature': temperature, 'max_tokens': max_tokens},
        _openai_headers(rt),
    )
    usage = data.get('usage', {})
    _record_telemetry(rt['provider'], model, usage.get('prompt_tokens', 0), usage.get('completion_tokens', 0))
    return (data['choices'][0]['message'].get('content') or '').strip()


def _openai_chat_tools(messages, tools, model, temperature, max_tokens, rt: dict):
    url = _openai_api_origin(rt).rstrip('/') + '/v1/chat/completions'
    # Ensure tool_calls in prior assistant messages include type/id for OpenAI
    norm_messages = []
    for m in messages:
        if isinstance(m, dict) and m.get('tool_calls'):
            tc_norm = []
            for idx, tc in enumerate(m.get('tool_calls') or []):
                tc = dict(tc)
                tc.setdefault('id', f'call_{idx}')
                tc.setdefault('type', 'function')
                tc_norm.append(tc)
            m = dict(m)
            m['tool_calls'] = tc_norm
        norm_messages.append(m)

    payload = {
        'model': model,
        'messages': norm_messages,
        'tools': tools,
        'temperature': temperature,
    }
    if max_tokens:
        if str(model).startswith('gpt-5'):
            payload['max_completion_tokens'] = max_tokens
        else:
            payload['max_tokens'] = max_tokens
    data = _post(
        url,
        payload,
        _openai_headers(rt),
        timeout=180,
    )
    choice_msg = data['choices'][0]['message']
    tool_calls = []
    for tc in (choice_msg.get('tool_calls') or []):
        args = tc['function']['arguments']
        # Keep OpenAI's raw JSON string for tool_calls in message history
        tool_calls.append({
            'id': tc.get('id'),
            'type': tc.get('type') or 'function',
            'function': {'name': tc['function']['name'], 'arguments': args},
        })
    usage = data.get('usage', {})
    _record_telemetry(rt['provider'], model, usage.get('prompt_tokens', 0), usage.get('completion_tokens', 0))
    return {
        'message': {
            'role': 'assistant',
            'content': choice_msg.get('content') or '',
            'tool_calls': tool_calls,
        }
    }


def _anthropic_headers(rt: dict):
    return {
        'x-api-key': rt['api_key'],
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


def _anthropic_chat(messages, model, temperature, max_tokens, rt: dict):
    system, rest = _split_system(messages)
    payload = {
        'model': model,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'messages': rest,
    }
    if system:
        payload['system'] = system
    data = _post('https://api.anthropic.com/v1/messages', payload, _anthropic_headers(rt))
    usage = data.get('usage', {})
    _record_telemetry(rt['provider'], model, usage.get('input_tokens', 0), usage.get('output_tokens', 0))
    for block in data.get('content', []):
        if block.get('type') == 'text':
            return block['text']
    return ''


def _openai_tools_to_anthropic(tools):
    out = []
    for t in tools:
        fn = t.get('function', {})
        out.append({
            'name': fn['name'],
            'description': fn.get('description', ''),
            'input_schema': fn.get('parameters', {'type': 'object', 'properties': {}}),
        })
    return out


def _anthropic_chat_tools(messages, tools, model, temperature, max_tokens, rt: dict):
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
    data = _post('https://api.anthropic.com/v1/messages', payload, _anthropic_headers(rt), timeout=180)
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
    usage = data.get('usage', {})
    _record_telemetry(rt['provider'], model, usage.get('input_tokens', 0), usage.get('output_tokens', 0))
    return {
        'message': {
            'role': 'assistant',
            'content': content_text,
            'tool_calls': tool_calls,
        }
    }


def chat(
    messages,
    model=None,
    *,
    temperature=0.3,
    max_tokens=8192,
    provider_override=None,
    base_url_override=None,
    api_key_override=_UNSET,
):
    """Call the configured LLM and return the assistant's text response.

    provider_override / base_url_override / api_key_override: optional per-call routing
    (e.g. task preferred_llm_provider / saved credential).
    """
    rt = _merge_runtime(provider_override, base_url_override, api_key_override)
    m = model or rt['default_model']
    prov = rt['provider']
    if prov == 'gemini':
        try:
            from workspace_llm_env import normalize_gemini_model_id

            m = normalize_gemini_model_id(m)
        except Exception:
            pass
    if prov == 'ollama':
        return _ollama_chat(messages, m, temperature, max_tokens, rt)
    if prov == 'anthropic':
        return _anthropic_chat(messages, m, temperature, max_tokens, rt)
    return _openai_chat(messages, m, temperature, max_tokens, rt)


def chat_with_tools(
    messages,
    tools,
    model=None,
    *,
    temperature=0.2,
    max_tokens=4096,
    provider_override=None,
    base_url_override=None,
    api_key_override=_UNSET,
):
    """Call the configured LLM with tool definitions."""
    rt = _merge_runtime(provider_override, base_url_override, api_key_override)
    m = model or rt['default_model']
    prov = rt['provider']
    if prov == 'gemini':
        try:
            from workspace_llm_env import normalize_gemini_model_id

            m = normalize_gemini_model_id(m)
        except Exception:
            pass
    if prov == 'ollama':
        return _ollama_chat_tools(messages, tools, m, temperature, max_tokens, rt)
    if prov == 'anthropic':
        return _anthropic_chat_tools(messages, tools, m, temperature, max_tokens, rt)
    return _openai_chat_tools(messages, tools, m, temperature, max_tokens, rt)

