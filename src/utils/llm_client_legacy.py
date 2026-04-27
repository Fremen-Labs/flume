#!/usr/bin/env python3
"""Flume LLM Client — stdlib only, zero third-party dependencies.

Provides a unified interface for multiple LLM providers. Configure via
environment variables:

  LLM_PROVIDER   : ollama | openai | openai_compatible | anthropic | gemini | xai | grok
                   Default: ollama
  LLM_BASE_URL   : Base URL for 'ollama' (default: http://localhost:11434)
                   or full base URL for 'openai_compatible'
  LLM_API_KEY    : API key for openai, anthropic, gemini, openai_compatible, xai, grok
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
  xai              : Uses https://api.x.ai/v1/chat/completions. Set LLM_API_KEY.
"""

import json
import os
import urllib.request
import urllib.error
import time

from utils.logger import get_logger

logger = get_logger("llm_client_legacy")

def _provider() -> str:
    return os.environ.get('LLM_PROVIDER', 'ollama').lower()


def _base_url_env() -> str:
    return os.environ.get('LLM_BASE_URL', 'http://localhost:11434').rstrip('/')


def _ollama_base_url(base_url_override=None) -> str:
    """Return the Ollama base URL without a /v1 suffix (Ollama uses /api/chat, not /v1/api/chat)."""
    raw = (base_url_override or _base_url_env()).rstrip('/')
    if raw.endswith('/v1'):
        raw = raw[:-3]
    return raw


def _api_key() -> str:
    return os.environ.get('LLM_API_KEY', '')


def _default_model() -> str:
    return os.environ.get('LLM_MODEL', 'llama3.2')

_PROVIDER_BASE_URLS = {
    'openai': 'https://api.openai.com',
    'anthropic': 'https://api.anthropic.com',
    'gemini': 'https://generativelanguage.googleapis.com/v1beta/openai',
    'xai': 'https://api.x.ai',
    'grok': 'https://api.x.ai',
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




# ---------------------------------------------------------------------------
# Thinking-model helpers
# ---------------------------------------------------------------------------

# Model name fragments that indicate a built-in reasoning/thinking mode.
# For these models Ollama will spend unbounded token budget on <think>...</think>
# blocks before emitting any visible content unless suppressed.
_THINKING_MODEL_FRAGMENTS = ('gemma3', 'gemma4', 'qwq', 'deepseek-r1', 'marco-o1')

# System message injected for thinking models when the caller has NOT opted in
# to the reasoning phase.  Acts as a prompt-level guard independently of any
# Ollama API `think` flag — works on Ollama versions that predate that option.
_NO_THINK_SYSTEM_MSG = (
    'IMPORTANT: Do NOT output any <think>...</think> reasoning blocks. '
    'Respond directly with your answer only — no chain-of-thought, no reasoning '
    'trace, no internal monologue. Begin your response immediately.'
)


def _is_thinking_model(model: str) -> bool:
    """Return True when *model* is a known reasoning/thinking Ollama model."""
    m = (model or '').lower().replace(':', '-').replace(' ', '-')
    return any(frag in m for frag in _THINKING_MODEL_FRAGMENTS)


def _inject_no_think_system(messages: list) -> list:
    """Prepend or augment a system message to suppress chain-of-thought output."""
    msgs = list(messages)
    if msgs and msgs[0].get('role') == 'system':
        msgs[0] = {**msgs[0], 'content': msgs[0]['content'] + '\n\n' + _NO_THINK_SYSTEM_MSG}
    else:
        msgs.insert(0, {'role': 'system', 'content': _NO_THINK_SYSTEM_MSG})
    return msgs


def _strip_think_blocks(text: str) -> str:
    """Remove any <think>...</think> blocks that leaked into the final content."""
    import re
    return re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.IGNORECASE).strip()


def _ollama_stream_strip_think(url: str, payload: dict, timeout: int) -> str:
    """Stream an Ollama NDJSON response, discarding <think>...</think> blocks.

    Ollama 0.x versions (pre-0.6.5) ignore the API-level `think=False` option.
    Streaming allows us to:
      1. Keep the HTTP connection alive — no N-minute blocking timeout waiting
         for a single huge non-streamed response.
      2. Actively discard <think> tokens in real time so they don't appear
         in the returned content.
      3. Start accumulating visible content the instant thinking ends.
    """
    import io
    headers = {'Content-Type': 'application/json'}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method='POST',
    )
    visible_parts: list = []
    in_think = False
    think_buf = ''

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw_line in io.TextIOWrapper(resp, encoding='utf-8'):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                chunk = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            token = chunk.get('message', {}).get('content', '')
            if not token:
                continue

            # State-machine to strip <think>...</think> spans that may cross
            # multiple token boundaries.
            combined = think_buf + token
            think_buf = ''
            while combined:
                if in_think:
                    end = combined.find('</think>')
                    if end == -1:
                        # Entire combined is still inside <think>, keep buffering
                        think_buf = combined
                        combined = ''
                    else:
                        in_think = False
                        combined = combined[end + len('</think>'):]
                else:
                    start = combined.find('<think>')
                    if start == -1:
                        visible_parts.append(combined)
                        combined = ''
                    else:
                        visible_parts.append(combined[:start])
                        in_think = True
                        combined = combined[start + len('<think>'):]

    return ''.join(visible_parts).strip()



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
    swapped_fallback = False
    
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_err = e
            
            if e.code in [400, 404] and not swapped_fallback and isinstance(payload, dict) and payload.get('model'):
                try:
                    from utils.llm_client_fallback import resolve_fallback_model
                    prov = "unknown"
                    if "openai.com" in url:
                        prov = "openai"
                    elif "anthropic.com" in url:
                        prov = "anthropic"
                    elif "generativelanguage" in url:
                        prov = "gemini"
                    elif "/api/" in url or ":11434" in url:
                        prov = "ollama"
                    
                    b_url = url.split('/api')[0] if prov == "ollama" else ""
                    fallback = resolve_fallback_model(prov, payload['model'], b_url)
                    if fallback:
                        logger.warning(f"Legacy Client: HTTP {e.code} for model '{payload['model']}'. Intelligently downgrading to '{fallback}'.")
                        payload['model'] = fallback
                        req.data = json.dumps(payload).encode()
                        swapped_fallback = True
                        continue
                except ImportError:
                    pass

            if e.code in [429, 500, 502, 503, 504]:
                time.sleep(2 ** attempt)
                continue
            model = None
            try:
                if isinstance(payload, dict):
                    model = payload.get('model')
            except Exception:
                model = None
            detail = f'HTTP {e.code} for {url}'
            if model:
                detail += f' (model={model})'
            raise RuntimeError(detail) from e
        except urllib.error.URLError as e:
            logger.warning(f"URLError connecting to LLM provider (attempt {attempt + 1}/{max_retries}): {e}. Retrying after {2 ** attempt}s...")
            last_err = e
            
            if not swapped_fallback and isinstance(payload, dict) and payload.get('model'):
                try:
                    from utils.llm_client_fallback import resolve_fallback_model
                    prov = "unknown"
                    if "openai.com" in url:
                        prov = "openai"
                    elif "anthropic.com" in url:
                        prov = "anthropic"
                    elif "generativelanguage" in url:
                        prov = "gemini"
                    elif "/api/" in url or ":11434" in url:
                        prov = "ollama"
                    
                    b_url = url.split('/api')[0] if prov == "ollama" else ""
                    fallback = resolve_fallback_model(prov, payload['model'], b_url)
                    if fallback:
                        logger.warning(f"Legacy Client: URLError for model '{payload['model']}'. Intelligently downgrading to '{fallback}'.")
                        payload['model'] = fallback
                        req.data = json.dumps(payload).encode()
                        swapped_fallback = True
                        continue
                except ImportError:
                    pass
                    
            time.sleep(2 ** attempt)
            continue
            
    if last_err:
        raise last_err


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def _ollama_chat(messages, model, temperature, max_tokens, base_url_override=None, timeout=120, ollama_think=False):
    base_url = _ollama_base_url(base_url_override=base_url_override)
    options: dict = {
        'temperature': temperature,
        'num_predict': max_tokens,
        # Increase context window from Ollama's 2048 default so large prompts
        # aren't silently truncated, which confuses thinking models.
        'num_ctx': int(os.environ.get('FLUME_OLLAMA_NUM_CTX', '8192')),
    }
    suppress_think = _is_thinking_model(model) and not ollama_think
    if suppress_think:
        # API-level flag: honoured by Ollama >= 0.6.5. Older versions ignore it
        # silently — the streaming fallback below handles those.
        options['think'] = False
        # Prompt-level guard: works on ALL Ollama versions regardless of API
        # support. Prepend a system message that explicitly forbids <think> output.
        messages = _inject_no_think_system(messages)

    if suppress_think:
        # Use streaming + real-time think-block stripping.
        # Older Ollama versions (< 0.6.5) ignore think=False in the API payload,
        # causing the model to spend 2-5 minutes on chain-of-thought before
        # emitting visible content.  With streaming we:
        #   • Stay connected — no timeout waiting for a huge non-streamed blob
        #   • Strip <think>...</think> spans as tokens arrive
        #   • Return visible content immediately after the thinking phase ends
        return _ollama_stream_strip_think(
            f'{base_url}/api/chat',
            {
                'model': model,
                'messages': messages,
                'stream': True,
                'options': options,
            },
            timeout=timeout,
        )

    data = _post(
        f'{base_url}/api/chat',
        {
            'model': model,
            'messages': messages,
            'stream': False,
            'options': options,
        },
        timeout=timeout,
    )
    return data.get('message', {}).get('content', '')


def _ollama_chat_tools(messages, tools, model, temperature, max_tokens, base_url_override=None, ollama_think=False):
    options: dict = {
        'temperature': temperature,
        'num_predict': max_tokens,
        'num_ctx': int(os.environ.get('FLUME_OLLAMA_NUM_CTX', '8192')),
    }
    suppress_think = _is_thinking_model(model) and not ollama_think
    if suppress_think:
        options['think'] = False
        messages = _inject_no_think_system(messages)
    return _post(
        f'{_ollama_base_url(base_url_override=base_url_override)}/api/chat',
        {
            'model': model,
            'messages': messages,
            'tools': tools,
            'stream': False,
            'options': options,
        },
        timeout=180,
    )


# ---------------------------------------------------------------------------
# OpenAI / OpenAI-compatible / Gemini
# ---------------------------------------------------------------------------


def _normalize_messages_for_openai(messages: list) -> list:
    """Ensure tool_calls arguments are JSON strings, not dicts.

    OpenAI and Gemini's OpenAI-compatible endpoints require
    ``tool_calls[].function.arguments`` to be a JSON *string*. The Flume
    agent loop stores them as parsed dicts after the first turn, which
    causes HTTP 400 'Value is not a string' on the follow-up request.
    """
    import copy
    out = []
    for m in messages:
        nm = dict(m)
        if 'tool_calls' in nm and isinstance(nm['tool_calls'], list):
            norm_calls = []
            for tc in nm['tool_calls']:
                tc = copy.deepcopy(tc)
                fn = tc.get('function', {})
                args = fn.get('arguments')
                if args is not None and not isinstance(args, str):
                    fn['arguments'] = json.dumps(args)
                norm_calls.append(tc)
            nm['tool_calls'] = norm_calls
        out.append(nm)
    return out

def _openai_headers():
    key = (_api_key() or '').strip()
    if not key:
        provider = _provider()
        if provider in ('openai', 'xai', 'grok'):
            raise RuntimeError(f'LLM_API_KEY is empty for managed provider {provider}.')
        key = 'sk-local-dummy-key'
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
    # Normalise messages: OpenAI/Gemini require tool_calls.function.arguments
    # to be JSON strings, but Python may have them as dicts from prior turns.
    norm_messages = _normalize_messages_for_openai(messages)
    data = _post(
        url,
        {
            'model': model,
            'messages': norm_messages,
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

def chat(messages, model=None, *, temperature=0.3, max_tokens=8192, provider_override=None, base_url_override=None, timeout_seconds=120, return_usage=False, ollama_think=False):
    """Call the configured LLM and return the assistant's text response.

    Args:
        messages:     List of {role, content} dicts in OpenAI format.
        model:        Model name override; falls back to LLM_MODEL env var.
        temperature:  Sampling temperature (0.0–1.0).
        max_tokens:   Maximum tokens to generate.
        ollama_think: If True, allow Ollama thinking-model reasoning phase.
                      Defaults to False (disabled) so gemma/qwq/deepseek-r1
                      don't silently spend 2+ minutes on <think> tokens.
                      Set FLUME_OLLAMA_THINK=1 env var to enable globally.

    Returns:
        str: The assistant's text response.
    """
    p = (provider_override or _provider()).lower()
    m = model or _default_model()
    if p == 'gemini':
        m = _normalize_gemini_model(m)
    # Honour FLUME_OLLAMA_THINK env override (allows per-deployment opt-in).
    effective_think = ollama_think or os.environ.get('FLUME_OLLAMA_THINK', '').strip() in ('1', 'true', 'yes')
    if p == 'ollama':
        content = _ollama_chat(messages, m, temperature, max_tokens, base_url_override, timeout=timeout_seconds, ollama_think=effective_think)
    elif p == 'anthropic':
        content = _anthropic_chat(messages, m, temperature, max_tokens)
    else:
        content = _openai_chat(messages, m, temperature, max_tokens, provider=p, base_url_override=base_url_override, timeout=timeout_seconds)
    if return_usage:
        return content, {}
    return content


def chat_with_tools(messages, tools, model=None, *, temperature=0.2, max_tokens=4096, provider_override=None, base_url_override=None, ollama_think=False):
    """Call the configured LLM with tool definitions.

    Args:
        messages:     List of {role, content} dicts in OpenAI format.
        tools:        List of tool definitions in OpenAI function-calling format.
        model:        Model name override; falls back to LLM_MODEL env var.
        temperature:  Sampling temperature.
        max_tokens:   Maximum tokens to generate.
        ollama_think: If True, allow Ollama thinking-model reasoning phase.
                      Defaults to False so tool-call loops don't stall.

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
    effective_think = ollama_think or os.environ.get('FLUME_OLLAMA_THINK', '').strip() in ('1', 'true', 'yes')
    if p == 'ollama':
        return _ollama_chat_tools(messages, tools, m, temperature, max_tokens, base_url_override, ollama_think=effective_think)
    elif p == 'anthropic':
        return _anthropic_chat_tools(messages, tools, m, temperature, max_tokens)
    else:
        return _openai_chat_tools(messages, tools, m, temperature, max_tokens, provider=p, base_url_override=base_url_override)
