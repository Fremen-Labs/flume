#!/usr/bin/env python3
"""Flume LLM Client — Gateway-backed thin HTTP shim.

Routes all LLM traffic through the Go flume-gateway service, which handles
provider routing, streaming, think-block stripping, and secret injection.

If the gateway is unreachable, falls back to direct provider calls for
development resilience.

Public API (unchanged from the original — zero call-site modifications):
  chat(messages, model=None, *, temperature=0.3, max_tokens=8192) -> str
  chat_with_tools(messages, tools, model=None, *, temperature=0.2, max_tokens=4096) -> dict
"""

import json
import os
import urllib.request
import urllib.error
import time

# ---------------------------------------------------------------------------
# Gateway connection
# ---------------------------------------------------------------------------

def _gateway_url() -> str:
    return os.environ.get('FLUME_GATEWAY_URL', 'http://gateway:8090').rstrip('/')


def _gateway_available() -> bool:
    """Quick health check — cached for 30s after first success."""
    global _gateway_ok, _gateway_checked_at
    now = time.monotonic()
    if _gateway_ok and (now - _gateway_checked_at) < 30:
        return True
    try:
        req = urllib.request.Request(f'{_gateway_url()}/health', method='GET')
        with urllib.request.urlopen(req, timeout=2) as resp:
            _gateway_ok = resp.status == 200
            _gateway_checked_at = now
            return _gateway_ok
    except Exception:
        _gateway_ok = False
        _gateway_checked_at = now
        return False

_gateway_ok = False
_gateway_checked_at = 0.0


def _post_gateway(path: str, payload: dict, timeout: int = 180) -> dict:
    """POST JSON to the gateway and return the parsed response."""
    url = f'{_gateway_url()}{path}'
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Legacy direct-call fallback (imported on demand if gateway is down)
# ---------------------------------------------------------------------------

_legacy_module = None


def _legacy():
    """Lazy-import the legacy llm_client implementation for fallback."""
    global _legacy_module
    if _legacy_module is not None:
        return _legacy_module
    import importlib.util
    from pathlib import Path
    legacy_path = Path(__file__).parent / 'llm_client_legacy.py'
    if not legacy_path.exists():
        raise RuntimeError(
            'flume-gateway is unreachable and no legacy fallback (llm_client_legacy.py) exists. '
            'Ensure the gateway container is running: docker compose up gateway'
        )
    spec = importlib.util.spec_from_file_location('llm_client_legacy', legacy_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    _legacy_module = mod
    return mod


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chat(
    messages,
    model=None,
    *,
    temperature=0.3,
    max_tokens=8192,
    provider_override=None,
    base_url_override=None,
    timeout_seconds=120,
    return_usage=False,
    ollama_think=False,
    agent_role='',
):
    """Call the configured LLM and return the assistant's text response.

    Args:
        messages:     List of {role, content} dicts in OpenAI format.
        model:        Model name override; falls back to LLM_MODEL env var.
        temperature:  Sampling temperature (0.0–1.0).
        max_tokens:   Maximum tokens to generate.
        ollama_think: If True, allow Ollama thinking-model reasoning phase.
        agent_role:   Agent role for multi-model routing (planner, implementer, etc.)

    Returns:
        str: The assistant's text response.
    """
    if _gateway_available():
        try:
            payload = {
                'messages': messages,
                'model': model or '',
                'provider': provider_override or '',
                'temperature': temperature,
                'max_tokens': max_tokens,
                'think': ollama_think,
                'agent_role': agent_role,
            }
            resp = _post_gateway('/v1/chat', payload, timeout=timeout_seconds)
            content = resp.get('message', {}).get('content', '')
            if return_usage:
                return content, resp.get('usage', {})
            return content
        except Exception as e:
            import logging
            leg = _legacy()
            p = provider_override or leg._provider()
            m = model or leg._default_model()
            b = base_url_override or leg._base_url(p, base_url_override)
            
            logger = logging.getLogger("llm_client")
            try:
                from utils.llm_client_fallback import resolve_fallback_model
                fallback = resolve_fallback_model(p, m, b)
            except ImportError:
                fallback = None

            if fallback:
                logger.warning(f"Gateway chat request failed for model '{m}': {e}. Intelligently downgrading to '{fallback}'.")
                try:
                    payload['model'] = fallback
                    resp = _post_gateway('/v1/chat', payload, timeout=timeout_seconds)
                    content = resp.get('message', {}).get('content', '')
                    if return_usage:
                        return content, resp.get('usage', {})
                    return content
                except Exception as e2:
                    logger.warning(f"Gateway fallback chat request also failed: {e2}. Proceeding to legacy client.")
            else:
                logger.warning(f"Gateway chat request failed: {e}. No fallback resolved. Proceeding to legacy client.")
            pass  # Fall through to legacy

    # Fallback to direct provider calls
    leg = _legacy()
    return leg.chat(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        provider_override=provider_override,
        base_url_override=base_url_override,
        timeout_seconds=timeout_seconds,
        return_usage=return_usage,
        ollama_think=ollama_think,
    )


def chat_with_tools(
    messages,
    tools,
    model=None,
    *,
    temperature=0.2,
    max_tokens=4096,
    provider_override=None,
    base_url_override=None,
    ollama_think=False,
    agent_role='',
):
    """Call the configured LLM with tool definitions.

    Args:
        messages:     List of {role, content} dicts in OpenAI format.
        tools:        List of tool definitions in OpenAI function-calling format.
        model:        Model name override; falls back to LLM_MODEL env var.
        temperature:  Sampling temperature.
        max_tokens:   Maximum tokens to generate.
        ollama_think: If True, allow Ollama thinking-model reasoning phase.
        agent_role:   Agent role for multi-model routing.

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
    if _gateway_available():
        try:
            payload = {
                'messages': messages,
                'tools': tools,
                'model': model or '',
                'provider': provider_override or '',
                'temperature': temperature,
                'max_tokens': max_tokens,
                'think': ollama_think,
                'agent_role': agent_role,
            }
            return _post_gateway('/v1/chat/tools', payload, timeout=180)
        except Exception as e:
            import logging
            leg = _legacy()
            p = provider_override or leg._provider()
            m = model or leg._default_model()
            b = base_url_override or leg._base_url(p, base_url_override)
            
            logger = logging.getLogger("llm_client")
            try:
                from utils.llm_client_fallback import resolve_fallback_model
                fallback = resolve_fallback_model(p, m, b)
            except ImportError:
                fallback = None

            if fallback:
                logger.warning(f"Gateway chat_with_tools request failed for model '{m}': {e}. Intelligently downgrading to '{fallback}'.")
                try:
                    payload['model'] = fallback
                    return _post_gateway('/v1/chat/tools', payload, timeout=180)
                except Exception as e2:
                    logger.warning(f"Gateway fallback chat_with_tools request also failed: {e2}. Proceeding to legacy client.")
            else:
                logger.warning(f"Gateway chat_with_tools request failed: {e}. No fallback resolved. Proceeding to legacy client.")
            pass  # Fall through to legacy

    leg = _legacy()
    return leg.chat_with_tools(
        messages,
        tools,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        provider_override=provider_override,
        base_url_override=base_url_override,
        ollama_think=ollama_think,
    )
