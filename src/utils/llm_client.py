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
import httpx
import asyncio

from utils.logger import get_logger

logger = get_logger("llm_client")

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


def _post_gateway(path: str, payload: dict, timeout: int = 180, max_retries: int = 3) -> dict:
    """POST JSON to the gateway and return the parsed response."""
    import random
    import socket
    url = f'{_gateway_url()}{path}'
    data = json.dumps(payload).encode()
    worker_name = os.environ.get('FLUME_WORKER_NAME', 'unknown')
    
    # Backoff sequence: 30s, 60s, 120s
    backoffs = [30, 60, 120]
    
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    'Content-Type': 'application/json',
                    'X-Worker-Name': worker_name
                },
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 402, 403, 404, 429):
                logger.error(f"Gateway rejected request (HTTP {e.code}): {e.read().decode('utf-8', 'ignore')}")
                raise Exception(f"Gateway HTTP {e.code}: {e.reason}") from e
            if attempt < max_retries:
                base_sleep = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
                # Jitter: +/- 10%
                jitter = base_sleep * 0.1 * (random.random() * 2 - 1)
                sleep_time = max(1.0, base_sleep + jitter)
                logger.warning(
                    f"Gateway HTTP error (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                    f"Applying intelligent backoff for {sleep_time:.1f}s."
                )
                time.sleep(sleep_time)
            else:
                logger.error(f"Gateway connection permanently failed after {max_retries} retries: {e}")
                raise e
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            if isinstance(e, socket.timeout) and timeout >= 60:
                logger.error(f"Gateway request timed out after {timeout}s. Not retrying as this indicates a slow LLM generation rather than a transient network error.")
                raise e
            if attempt < max_retries:
                base_sleep = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
                jitter = base_sleep * 0.1 * (random.random() * 2 - 1)
                sleep_time = max(1.0, base_sleep + jitter)
                logger.warning(
                    f"Gateway connection issue logic bounds (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                    f"Applying intelligent backoff for {sleep_time:.1f}s to relieve Node Mesh gridlock."
                )
                time.sleep(sleep_time)
            else:
                logger.error(f"Gateway connection permanently failed after {max_retries} retries: {e}")
                raise e


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
    return_telemetry=False,
    ollama_think=False,
    agent_role='',
    task_id=None,
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
            if task_id:
                payload['task_id'] = task_id
            resp = _post_gateway('/v1/chat', payload, timeout=timeout_seconds)
            
            telemetry = resp.get('telemetry', {})
            if telemetry:
                logger.info("Gateway telemetry retrieved: node_id=%s, node_host=%s", 
                            telemetry.get('node_id'), telemetry.get('node_host'))
            
            content = resp.get('message', {}).get('content', '')
            if return_telemetry and return_usage:
                return content, resp.get('usage', {}), resp.get('telemetry', {})
            if return_telemetry:
                return content, resp.get('telemetry', {})
            if return_usage:
                return content, resp.get('usage', {})
            return content
        except Exception as e:
            leg = _legacy()
            p = provider_override or leg._provider()
            m = model or leg._default_model()
            b = base_url_override or leg._base_url(p, base_url_override)
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
                    if return_telemetry and return_usage:
                        return content, resp.get('usage', {}), resp.get('telemetry', {})
                    if return_telemetry:
                        return content, resp.get('telemetry', {})
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
    leg_result = leg.chat(
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
    if return_telemetry and return_usage:
        return leg_result[0], leg_result[1], {}
    if return_telemetry:
        return leg_result, {}
    return leg_result


def chat_with_tools(
    messages,
    tools,
    model=None,
    *,
    temperature=0.2,
    max_tokens=4096,
    provider_override=None,
    base_url_override=None,
    return_telemetry=False,
    ollama_think=False,
    agent_role='',
    task_id=None,
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
            if task_id:
                payload['task_id'] = task_id
            resp = _post_gateway('/v1/chat/tools', payload, timeout=180)
            
            telemetry = resp.get('telemetry', {})
            if telemetry:
                logger.info("Gateway telemetry retrieved (tools): node_id=%s, node_host=%s", 
                            telemetry.get('node_id'), telemetry.get('node_host'))
            
            if return_telemetry:
                return resp, resp.get('telemetry', {})
            return resp
        except Exception as e:
            leg = _legacy()
            p = provider_override or leg._provider()
            m = model or leg._default_model()
            b = base_url_override or leg._base_url(p, base_url_override)
            try:
                from utils.llm_client_fallback import resolve_fallback_model
                fallback = resolve_fallback_model(p, m, b)
            except ImportError:
                fallback = None

            if fallback:
                logger.warning(f"Gateway chat_with_tools request failed for model '{m}': {e}. Intelligently downgrading to '{fallback}'.")
                try:
                    payload['model'] = fallback
                    resp = _post_gateway('/v1/chat/tools', payload, timeout=180)
                    if return_telemetry:
                        return resp, resp.get('telemetry', {})
                    return resp
                except Exception as e2:
                    logger.warning(f"Gateway fallback chat_with_tools request also failed: {e2}. Proceeding to legacy client.")
            else:
                logger.warning(f"Gateway chat_with_tools request failed: {e}. No fallback resolved. Proceeding to legacy client.")
            pass  # Fall through to legacy

    leg = _legacy()
    leg_result = leg.chat_with_tools(
        messages,
        tools,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        provider_override=provider_override,
        base_url_override=base_url_override,
        ollama_think=ollama_think,
    )
    if return_telemetry:
        return leg_result, {}
    return leg_result

# ---------------------------------------------------------------------------
# Async API
# ---------------------------------------------------------------------------

async def _post_gateway_async(client: httpx.AsyncClient, path: str, payload: dict, timeout: int = 180, max_retries: int = 3) -> dict:
    import random
    url = f'{_gateway_url()}{path}'
    worker_name = os.environ.get('FLUME_WORKER_NAME', 'unknown')
    backoffs = [30, 60, 120]
    
    for attempt in range(max_retries + 1):
        try:
            resp = await client.post(
                url,
                json=payload,
                headers={'X-Worker-Name': worker_name},
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 401, 402, 403, 404, 429):
                logger.error(f"Gateway rejected request (HTTP {e.response.status_code}): {e.response.text}")
                raise Exception(f"Gateway HTTP {e.response.status_code}: {e.response.reason_phrase}") from e
            if attempt < max_retries:
                base_sleep = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
                jitter = base_sleep * 0.1 * (random.random() * 2 - 1)
                sleep_time = max(1.0, base_sleep + jitter)
                logger.warning(f"Gateway HTTP error (attempt {attempt + 1}/{max_retries + 1}): {e}. Backing off {sleep_time:.1f}s.")
                await asyncio.sleep(sleep_time)
            else:
                logger.error(f"Gateway connection permanently failed: {e}")
                raise e
        except httpx.RequestError as e:
            if isinstance(e, httpx.ReadTimeout) and timeout >= 60:
                logger.error(f"Gateway request timed out after {timeout}s.")
                raise e
            if attempt < max_retries:
                base_sleep = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
                jitter = base_sleep * 0.1 * (random.random() * 2 - 1)
                sleep_time = max(1.0, base_sleep + jitter)
                logger.warning(f"Gateway connection issue (attempt {attempt + 1}/{max_retries + 1}): {e}. Backing off {sleep_time:.1f}s.")
                await asyncio.sleep(sleep_time)
            else:
                logger.error(f"Gateway connection permanently failed: {e}")
                raise e

async def chat_async(
    client: httpx.AsyncClient,
    messages,
    model=None,
    *,
    temperature=0.3,
    max_tokens=8192,
    provider_override=None,
    base_url_override=None,
    timeout_seconds=120,
    return_usage=False,
    return_telemetry=False,
    ollama_think=False,
    agent_role='',
    task_id=None,
):
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
            if task_id:
                payload['task_id'] = task_id
            resp = await _post_gateway_async(client, '/v1/chat', payload, timeout=timeout_seconds)
            
            telemetry = resp.get('telemetry', {})
            if telemetry:
                logger.info("Gateway telemetry retrieved: node_id=%s, node_host=%s", 
                            telemetry.get('node_id'), telemetry.get('node_host'))
            
            content = resp.get('message', {}).get('content', '')
            if return_telemetry and return_usage:
                return content, resp.get('usage', {}), resp.get('telemetry', {})
            if return_telemetry:
                return content, resp.get('telemetry', {})
            if return_usage:
                return content, resp.get('usage', {})
            return content
        except Exception as e:
            leg = _legacy()
            p = provider_override or leg._provider()
            m = model or leg._default_model()
            b = base_url_override or leg._base_url(p, base_url_override)
            try:
                from utils.llm_client_fallback import resolve_fallback_model
                fallback = resolve_fallback_model(p, m, b)
            except ImportError:
                fallback = None

            if fallback:
                logger.warning(f"Gateway chat request failed for model '{m}': {e}. Intelligently downgrading to '{fallback}'.")
                try:
                    payload['model'] = fallback
                    resp = await _post_gateway_async(client, '/v1/chat', payload, timeout=timeout_seconds)
                    content = resp.get('message', {}).get('content', '')
                    if return_telemetry and return_usage:
                        return content, resp.get('usage', {}), resp.get('telemetry', {})
                    if return_telemetry:
                        return content, resp.get('telemetry', {})
                    if return_usage:
                        return content, resp.get('usage', {})
                    return content
                except Exception as e2:
                    logger.warning(f"Gateway fallback chat request also failed: {e2}. Proceeding to legacy client.")
            else:
                logger.warning(f"Gateway chat request failed: {e}. No fallback resolved. Proceeding to legacy client.")
            pass  # Fall through to legacy

    # Fallback to direct provider calls via thread pool
    leg = _legacy()
    leg_result = await asyncio.to_thread(
        leg.chat,
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
    if return_telemetry and return_usage:
        return leg_result[0], leg_result[1], {}
    if return_telemetry:
        return leg_result, {}
    return leg_result


async def chat_with_tools_async(
    client: httpx.AsyncClient,
    messages,
    tools,
    model=None,
    *,
    temperature=0.2,
    max_tokens=4096,
    provider_override=None,
    base_url_override=None,
    return_telemetry=False,
    ollama_think=False,
    agent_role='',
    task_id=None,
):
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
            if task_id:
                payload['task_id'] = task_id
            resp = await _post_gateway_async(client, '/v1/chat/tools', payload, timeout=180)
            
            telemetry = resp.get('telemetry', {})
            if telemetry:
                logger.info("Gateway telemetry retrieved (tools): node_id=%s, node_host=%s", 
                            telemetry.get('node_id'), telemetry.get('node_host'))
            
            if return_telemetry:
                return resp, resp.get('telemetry', {})
            return resp
        except Exception as e:
            leg = _legacy()
            p = provider_override or leg._provider()
            m = model or leg._default_model()
            b = base_url_override or leg._base_url(p, base_url_override)
            try:
                from utils.llm_client_fallback import resolve_fallback_model
                fallback = resolve_fallback_model(p, m, b)
            except ImportError:
                fallback = None

            if fallback:
                logger.warning(f"Gateway chat_with_tools request failed for model '{m}': {e}. Intelligently downgrading to '{fallback}'.")
                try:
                    payload['model'] = fallback
                    resp = await _post_gateway_async(client, '/v1/chat/tools', payload, timeout=180)
                    if return_telemetry:
                        return resp, resp.get('telemetry', {})
                    return resp
                except Exception as e2:
                    logger.warning(f"Gateway fallback chat_with_tools request also failed: {e2}. Proceeding to legacy client.")
            else:
                logger.warning(f"Gateway chat_with_tools request failed: {e}. No fallback resolved. Proceeding to legacy client.")
            pass  # Fall through to legacy

    # Fallback to direct provider calls via thread pool
    leg = _legacy()
    leg_result = await asyncio.to_thread(
        leg.chat_with_tools,
        messages,
        tools,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        provider_override=provider_override,
        base_url_override=base_url_override,
        ollama_think=ollama_think,
    )
    if return_telemetry:
        return leg_result, {}
    return leg_result

