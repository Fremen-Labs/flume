"""Core REST adapter for Elasticsearch interactions (sync/async) and bulk buffering.

This module is the single point of contact between the Flume dashboard and
Elasticsearch.  It provides:

- **Lazy configuration** — ES URL, credentials, and TLS settings are resolved
  on first use (not at import time), enabling clean unit testing and safe
  monkeypatching.
- **Async-canonical API** — Business logic lives in the async (httpx) layer.
  Sync helpers exist only for background threads that cannot use asyncio.
- **Bulk buffering** — High-throughput task-record updates are coalesced into
  ``_bulk`` requests via a lock-protected buffer and a dedicated flusher thread.
"""
import asyncio
import base64
import functools
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional, Tuple, Union

import httpx

from utils.exceptions import SAFE_EXCEPTIONS
from utils.logger import get_logger

logger = get_logger(__name__)


# ── Lazy configuration ───────────────────────────────────────────────────────
#
# All ES connectivity settings are resolved on first access via _get_es_config().
# This eliminates module-level side effects that previously made the module
# impossible to import in test harnesses without env-var gymnastics.


@dataclass(frozen=True)
class _ESConfig:
    """Immutable, lazily-resolved Elasticsearch connectivity descriptor."""

    url: str
    api_key: str
    password: str
    verify_tls: bool
    ssl_ctx: Optional[ssl.SSLContext] = field(default=None, repr=False)

    # ── Derived helpers ──────────────────────────────────────────────────

    def auth_headers(self) -> dict:
        """Return the appropriate Authorization header dict."""
        if self.api_key:
            return {"Authorization": f"ApiKey {self.api_key}"}
        if self.password:
            b64 = base64.b64encode(f"elastic:{self.password}".encode()).decode()
            return {"Authorization": f"Basic {b64}"}
        return {}

    def httpx_verify(self) -> Union[bool, ssl.SSLContext]:
        """Return the ``verify`` kwarg for ``httpx.AsyncClient``."""
        if self.ssl_ctx is not None:
            if not self.verify_tls:
                return False
            return self.ssl_ctx
        return True


@functools.lru_cache(maxsize=1)
def _get_es_config() -> _ESConfig:
    """Resolve ES configuration exactly once, lazily on first call.

    Reads from the Pydantic ``AppConfig`` singleton, which itself reads
    environment variables and ``.env`` files.  The ``lru_cache`` ensures this
    work is performed only once per process lifetime.
    """
    from config import get_settings  # deferred to break import-time side effects

    settings = get_settings()

    if settings.FLUME_NATIVE_MODE == "1":
        default_es = "http://localhost:9200"
    else:
        default_es = "http://elasticsearch:9200"

    url = settings.ES_URL.rstrip("/") if settings.ES_URL else default_es
    verify_tls: bool = settings.ES_VERIFY_TLS

    # Build SSL context only when the URL is HTTPS.
    ssl_ctx: Optional[ssl.SSLContext] = None
    if url.startswith("https:"):
        ssl_ctx = ssl.create_default_context()
        if not verify_tls:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

    return _ESConfig(
        url=url,
        api_key=settings.ES_API_KEY,
        password=settings.FLUME_ELASTIC_PASSWORD,
        verify_tls=verify_tls,
        ssl_ctx=ssl_ctx,
    )


# ── Public accessors (backward-compatible symbols) ───────────────────────────
#
# External modules (counters.py, projects_store.py, project_lifecycle.py,
# system_status.py, server.py) previously imported bare globals:
#   ES_URL, ES_API_KEY, ctx, _get_auth_headers, _get_httpx_verify
#
# These thin accessors preserve the same call-site semantics while delegating
# to the lazy _ESConfig singleton.


def get_es_url() -> str:
    """Return the resolved Elasticsearch base URL (no trailing slash)."""
    return _get_es_config().url


def get_ssl_context() -> Optional[ssl.SSLContext]:
    """Return the SSL context for urllib calls, or ``None`` for plain HTTP."""
    return _get_es_config().ssl_ctx


def _get_auth_headers() -> dict:
    """Return the Authorization header dict for ES requests."""
    return _get_es_config().auth_headers()


def _get_httpx_verify() -> Union[bool, ssl.SSLContext]:
    """Return the ``verify`` kwarg suitable for ``httpx.AsyncClient``."""
    return _get_es_config().httpx_verify()


# Backward-compatible property-style aliases so existing ``from core.elasticsearch
# import ES_URL, ctx, ES_API_KEY`` statements continue to resolve at call time.
# These are module-level *names* whose values are resolved lazily via __getattr__.

def __getattr__(name: str):
    """Lazy module-level attribute access for backward-compatible globals.

    Supports: ``ES_URL``, ``ES_API_KEY``, ``ES_PASSWORD``, ``ES_VERIFY_TLS``, ``ctx``.
    """
    _LAZY_MAP = {
        "ES_URL": lambda: _get_es_config().url,
        "ES_API_KEY": lambda: _get_es_config().api_key,
        "ES_PASSWORD": lambda: _get_es_config().password,
        "ES_VERIFY_TLS": lambda: _get_es_config().verify_tls,
        "ctx": lambda: _get_es_config().ssl_ctx,
    }
    if name in _LAZY_MAP:
        return _LAZY_MAP[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ── Shared helpers (DRY: used by both sync and async layers) ─────────────────

def _build_json_headers() -> dict:
    """Build standard JSON content-type + auth headers for an ES request."""
    headers = {"Content-Type": "application/json"}
    headers.update(_get_auth_headers())
    return headers


# ── Async-canonical ES operations ────────────────────────────────────────────
#
# These are the *single source of truth* for each ES operation.  Sync wrappers
# below delegate to the synchronous urllib layer (``es_post``) which exists
# solely because the bulk-flusher thread cannot run in an asyncio event loop.


async def async_es_search(index: str, body: dict) -> dict:
    """Execute an ES ``_search`` request.  Returns ``{}`` on 404 (missing index)."""
    cfg = _get_es_config()
    headers = _build_json_headers()
    async with httpx.AsyncClient(verify=cfg.httpx_verify()) as client:
        resp = await client.post(
            f"{cfg.url}/{index}/_search",
            json=body,
            headers=headers,
        )
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json()


async def async_es_index(index: str, doc: dict) -> dict:
    """Index (create) a new document in *index*."""
    cfg = _get_es_config()
    headers = _build_json_headers()
    async with httpx.AsyncClient(verify=cfg.httpx_verify()) as client:
        resp = await client.post(
            f"{cfg.url}/{index}/_doc",
            json=doc,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


async def async_es_upsert(index: str, doc_id: str, doc: dict) -> dict:
    """Create-or-replace document *doc_id* in *index* via PUT."""
    cfg = _get_es_config()
    headers = _build_json_headers()
    safe_id = urllib.parse.quote(str(doc_id), safe="")
    async with httpx.AsyncClient(verify=cfg.httpx_verify()) as client:
        resp = await client.put(
            f"{cfg.url}/{index}/_doc/{safe_id}",
            json=doc,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


async def async_es_delete_doc(index: str, doc_id: str) -> bool:
    """Delete document *doc_id*.  Returns ``True`` on success or 404 (idempotent)."""
    cfg = _get_es_config()
    safe_id = urllib.parse.quote(str(doc_id), safe="")
    headers = _build_json_headers()
    async with httpx.AsyncClient(verify=cfg.httpx_verify()) as client:
        try:
            resp = await client.delete(
                f"{cfg.url}/{index}/_doc/{safe_id}",
                headers=headers,
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return True
            raise


async def async_es_post(path: str, body: dict, method: str = "POST") -> dict:
    """Generic ES request with exponential-backoff retry on 429/503/504."""
    cfg = _get_es_config()
    headers = _build_json_headers()
    async with httpx.AsyncClient(verify=cfg.httpx_verify()) as client:
        for attempt in range(4):
            try:
                resp = await client.request(
                    method=method,
                    url=f"{cfg.url}/{path}",
                    json=body,
                    headers=headers,
                    timeout=10.0,
                )
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 503, 504) and attempt < 3:
                    await asyncio.sleep((2 ** attempt) * 0.1)
                    continue
                raise
            except httpx.RequestError:
                if attempt < 3:
                    await asyncio.sleep((2 ** attempt) * 0.1)
                    continue
                raise
    return {}  # unreachable but satisfies type checkers


async def async_find_task_doc_by_logical_id(
    logical_id: str,
) -> Tuple[Optional[str], Optional[dict]]:
    """Locate a task document by trying multiple query strategies."""
    tid = (logical_id or "").strip()
    if not tid:
        return None, None
    attempts = [
        {"ids": {"values": [tid]}},
        {"term": {"id": tid}},
        {"term": {"id.keyword": tid}},
        {"match_phrase": {"id": tid}},
    ]
    for query in attempts:
        try:
            res = await async_es_search(
                "agent-task-records", {"size": 1, "query": query}
            )
            hits = res.get("hits", {}).get("hits", [])
            if hits:
                h = hits[0]
                return h.get("_id"), h.get("_source", {})
        except SAFE_EXCEPTIONS + (httpx.RequestError, httpx.HTTPStatusError):
            continue
    return None, None


# ── Sync wrappers (urllib — for background threads only) ─────────────────────
#
# The bulk-flusher thread and a handful of legacy callers cannot use asyncio.
# These thin sync functions use urllib and delegate shared logic to helpers.


def es_post(path: str, body: dict, method: str = "POST") -> dict:
    """Synchronous generic ES request with exponential-backoff retry."""
    cfg = _get_es_config()
    headers = _build_json_headers()
    req = urllib.request.Request(
        f"{cfg.url}/{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method=method,
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, context=cfg.ssl_ctx, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 504) and attempt < 3:
                time.sleep((2 ** attempt) * 0.1)
                continue
            raise
        except SAFE_EXCEPTIONS:
            if attempt < 3:
                time.sleep((2 ** attempt) * 0.1)
                continue
            raise
    return {}  # unreachable but satisfies type checkers


def es_search(index: str, body: dict) -> dict:
    """Synchronous ES search.  Returns ``{}`` on 404."""
    cfg = _get_es_config()
    headers = _build_json_headers()
    req = urllib.request.Request(
        f"{cfg.url}/{index}/_search",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=cfg.ssl_ctx, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def es_index(index: str, doc: dict) -> dict:
    """Synchronous document index (create)."""
    cfg = _get_es_config()
    headers = _build_json_headers()
    req = urllib.request.Request(
        f"{cfg.url}/{index}/_doc",
        data=json.dumps(doc).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, context=cfg.ssl_ctx, timeout=10) as resp:
        return json.loads(resp.read().decode())


def es_upsert(index: str, doc_id: str, doc: dict) -> dict:
    """Synchronous document create-or-replace via PUT."""
    cfg = _get_es_config()
    headers = _build_json_headers()
    safe_id = urllib.parse.quote(str(doc_id), safe="")
    req = urllib.request.Request(
        f"{cfg.url}/{index}/_doc/{safe_id}",
        data=json.dumps(doc).encode(),
        headers=headers,
        method="PUT",
    )
    with urllib.request.urlopen(req, context=cfg.ssl_ctx, timeout=10) as resp:
        return json.loads(resp.read().decode())


def es_delete_doc(index: str, doc_id: str) -> bool:
    """Synchronous document delete.  Returns ``False`` on 404."""
    cfg = _get_es_config()
    safe_id = urllib.parse.quote(str(doc_id), safe="")
    headers = _build_json_headers()
    req = urllib.request.Request(
        f"{cfg.url}/{index}/_doc/{safe_id}",
        headers=headers,
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, context=cfg.ssl_ctx, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def find_task_doc_by_logical_id(
    logical_id: str,
) -> Tuple[Optional[str], Optional[dict]]:
    """Synchronous task document lookup using cascading query strategies."""
    tid = (logical_id or "").strip()
    if not tid:
        return None, None
    attempts = [
        {"ids": {"values": [tid]}},
        {"term": {"id": tid}},
        {"term": {"id.keyword": tid}},
        {"match_phrase": {"id": tid}},
    ]
    for query in attempts:
        try:
            hits = (
                es_search("agent-task-records", {"size": 1, "query": query})
                .get("hits", {})
                .get("hits", [])
            )
            if hits:
                h = hits[0]
                return h.get("_id"), h.get("_source", {})
        except SAFE_EXCEPTIONS:
            continue
    return None, None


# ── Bulk Buffering & Connection Resiliency ───────────────────────────────────

_ES_BULK_BUFFER: list = []
_ES_BULK_LOCK = threading.Lock()
_ES_BULK_LAST_FLUSH = time.time()


def _es_bulk_flusher_loop() -> None:
    """Background thread: periodically flush the bulk update buffer to ES.

    Flushes when the buffer reaches 50 items **or** 250 ms have elapsed since
    the last flush — whichever comes first.
    """
    global _ES_BULK_LAST_FLUSH
    while True:
        time.sleep(0.05)
        with _ES_BULK_LOCK:
            now = time.time()
            if len(_ES_BULK_BUFFER) >= 50 or (
                len(_ES_BULK_BUFFER) > 0
                and (now - _ES_BULK_LAST_FLUSH) >= 0.25
            ):
                _flush_es_bulk_unlocked()


def _flush_es_bulk_unlocked() -> None:
    """Flush all buffered bulk updates to ES.  Caller MUST hold ``_ES_BULK_LOCK``."""
    global _ES_BULK_BUFFER, _ES_BULK_LAST_FLUSH
    if not _ES_BULK_BUFFER:
        return
    payloads = _ES_BULK_BUFFER
    _ES_BULK_BUFFER = []
    _ES_BULK_LAST_FLUSH = time.time()

    # Build NDJSON via list accumulator (O(n) vs O(n²) string concat)
    lines: list[str] = []
    for op in payloads:
        lines.append(
            json.dumps({"update": {"_index": op["index"], "_id": op["id"]}})
        )
        lines.append(json.dumps({"doc": op["doc"]}))
    lines.append("")  # trailing newline required by _bulk API
    ndjson = "\n".join(lines)

    cfg = _get_es_config()
    headers = {"Content-Type": "application/x-ndjson"}
    headers.update(cfg.auth_headers())
    req = urllib.request.Request(
        f"{cfg.url}/_bulk",
        data=ndjson.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, context=cfg.ssl_ctx, timeout=15):
                pass
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 504) and attempt < 3:
                time.sleep((2 ** attempt) * 0.1)
                continue
            logger.warning(
                "ES Bulk HTTP Flush failed",
                extra={
                    "structured_data": {
                        "event": "es_bulk_http_flush_failed",
                        "error": str(e),
                        "attempt": attempt + 1,
                        "payloads": len(payloads),
                    }
                },
            )
            break
        except SAFE_EXCEPTIONS as e:
            if attempt < 3:
                time.sleep((2 ** attempt) * 0.1)
                continue
            logger.warning(
                "ES Bulk Connection failed",
                extra={
                    "structured_data": {
                        "event": "es_bulk_connection_failed",
                        "error": str(e),
                        "attempt": attempt + 1,
                        "payloads": len(payloads),
                    }
                },
            )
            break


def es_bulk_update_proxy(path: str, body: dict, method: str = "POST") -> dict:
    """Intercept task-record updates and buffer them for bulk flushing.

    Non-matching requests are forwarded to ``es_post`` synchronously.
    """
    if method == "POST" and path.startswith("agent-task-records/_update/"):
        doc_id = path.split("/")[-1]
        doc = body.get("doc", {})
        if doc:
            with _ES_BULK_LOCK:
                _ES_BULK_BUFFER.append(
                    {"index": "agent-task-records", "id": doc_id, "doc": doc}
                )
            return {"status": "buffered"}
    return es_post(path, body, method)
