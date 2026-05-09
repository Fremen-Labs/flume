"""Unified, connection-pooled Elasticsearch client for the Flume worker-manager.

Replaces the duplicate implementations:
  - manager.py:  httpx.Client-based es_request() (L385-392)
  - worker_handlers.py:  urllib.request-based es_request() (L90-101)
  - worker_handlers.py:  urllib.request-based _es_projects_request_worker() (L255-275)

All modules should import from here:
    from es.client import es_request, es_request_raw
"""
import httpx

from config import ES_URL, ES_VERIFY_TLS
from utils.es_auth import get_es_auth_headers
from utils.logger import get_logger

logger = get_logger('es.client')

# ── Singleton Connection-Pooled Client ───────────────────────────────────────
# Phase 4: HTTP keep-alive eliminates TLS handshake overhead (~15ms/connection).
_ES_CLIENT: httpx.Client = httpx.Client(
    base_url=ES_URL,
    headers=get_es_auth_headers(),
    verify=ES_VERIFY_TLS,
    timeout=httpx.Timeout(30.0, connect=5.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)


def get_es_client() -> httpx.Client:
    """Return the module-level ES client singleton (for advanced usage)."""
    return _ES_CLIENT


def es_request(path: str, body: dict = None, method: str = 'POST') -> dict:
    """Send a JSON request to ES via the connection-pooled httpx.Client."""
    if body is not None and method == 'GET':
        # ES expects POST for JSON search bodies; GET+body is unreliable behind proxies.
        method = 'POST'
    resp = _ES_CLIENT.request(method, path, json=body)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def es_request_raw(path: str, raw_body: str, method: str = 'POST') -> dict:
    """Send a raw string body to ES (e.g. for _bulk NDJSON)."""
    resp = _ES_CLIENT.request(
        method, path,
        content=raw_body.encode(),
        headers={'Content-Type': 'application/x-ndjson'},
    )
    resp.raise_for_status()
    return resp.json() if resp.content else {}
