"""Core REST adapter for Elasticsearch interactions (sync/async) and bulk buffering.

This module is the single point of contact between the Flume dashboard and
Elasticsearch.  It provides:

- **Lazy configuration** — ES URL, credentials, and TLS settings are resolved
  on first use (not at import time), enabling clean unit testing and safe
  monkeypatching.
- **Async-canonical API** — Business logic lives in the async (httpx) layer.
  Sync helpers exist only for background threads that cannot use asyncio.
- **Persistent async client** — A module-scoped ``httpx.AsyncClient`` with
  connection pooling eliminates per-request TLS handshake overhead.
- **Bulk buffering** — High-throughput task-record updates are coalesced into
  ``_bulk`` requests via a ``BulkFlusher`` class with event-driven wakeup.
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


# ── Named constants (Finding 10) ────────────────────────────────────────────
#
# Previously hard-coded throughout the module; now centralized for tuning.

_MAX_RETRIES: int = 4
"""Maximum number of retry attempts for retryable ES errors (429/503/504)."""

_BACKOFF_BASE_S: float = 0.1
"""Base multiplier for exponential backoff: delay = 2^attempt × base."""

_REQUEST_TIMEOUT_S: float = 10.0
"""Default timeout for individual ES HTTP requests (sync and async)."""

_BULK_FLUSH_TIMEOUT_S: float = 15.0
"""Timeout for the ``_bulk`` HTTP request during buffer flush."""

_BULK_FLUSH_THRESHOLD: int = 50
"""Flush the bulk buffer when it contains this many items."""

_BULK_MAX_AGE_S: float = 0.25
"""Maximum age (seconds) of the oldest buffered item before forced flush."""

_TASK_RECORDS_INDEX: str = "agent-task-records"
"""Canonical ES index name for task records."""


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


# ── Persistent httpx.AsyncClient (Finding 3) ────────────────────────────────
#
# Creating a new AsyncClient per request wastes a TLS handshake (~50-100ms)
# and prevents HTTP/2 multiplexing and TCP keepalive.  A module-scoped
# singleton is lazily created on first use and reused for all async operations.

_httpx_client: Optional[httpx.AsyncClient] = None
_httpx_client_lock = threading.Lock()


def _get_async_client() -> httpx.AsyncClient:
    """Return a persistent ``httpx.AsyncClient``, creating it lazily if needed.

    Thread-safe via a lock.  The client is configured with connection pooling
    limits suitable for a single-node dashboard.  If the client was closed
    externally (e.g., during ASGI shutdown), a new one is created transparently.
    """
    global _httpx_client
    with _httpx_client_lock:
        if _httpx_client is None or _httpx_client.is_closed:
            cfg = _get_es_config()
            _httpx_client = httpx.AsyncClient(
                verify=cfg.httpx_verify(),
                timeout=httpx.Timeout(_REQUEST_TIMEOUT_S, connect=5.0),
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                ),
            )
    return _httpx_client


async def close_async_client() -> None:
    """Gracefully close the persistent httpx client.

    Called during ASGI lifespan shutdown to release pooled connections.
    """
    global _httpx_client
    with _httpx_client_lock:
        if _httpx_client is not None and not _httpx_client.is_closed:
            await _httpx_client.aclose()
            _httpx_client = None


# ── Async-canonical ES operations ────────────────────────────────────────────
#
# These are the *single source of truth* for each ES operation.  Sync wrappers
# below delegate to the synchronous urllib layer (``es_post``) which exists
# solely because the bulk-flusher thread cannot run in an asyncio event loop.


async def async_es_search(index: str, body: dict) -> dict:
    """Execute an ES ``_search`` request.  Returns ``{}`` on 404 (missing index)."""
    cfg = _get_es_config()
    headers = _build_json_headers()
    client = _get_async_client()
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
    client = _get_async_client()
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
    client = _get_async_client()
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
    client = _get_async_client()
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
    client = _get_async_client()
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await client.request(
                method=method,
                url=f"{cfg.url}/{path}",
                json=body,
                headers=headers,
                timeout=_REQUEST_TIMEOUT_S,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 503, 504) and attempt < _MAX_RETRIES - 1:
                await asyncio.sleep((2 ** attempt) * _BACKOFF_BASE_S)
                continue
            raise
        except httpx.RequestError:
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep((2 ** attempt) * _BACKOFF_BASE_S)
                continue
            raise
    return {}  # unreachable but satisfies type checkers


async def async_find_task_doc_by_logical_id(
    logical_id: str,
) -> Tuple[Optional[str], Optional[dict]]:
    """Locate a task document via a single ``bool.should`` query.

    Combines all lookup strategies (IDs, term, term.keyword, match_phrase)
    into one ES round-trip instead of up to 4 sequential requests (Finding 11).
    """
    tid = (logical_id or "").strip()
    if not tid:
        return None, None
    try:
        res = await async_es_search(
            _TASK_RECORDS_INDEX,
            {
                "size": 1,
                "query": {
                    "bool": {
                        "should": [
                            {"ids": {"values": [tid]}},
                            {"term": {"id": tid}},
                            {"term": {"id.keyword": tid}},
                            {"match_phrase": {"id": tid}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
            },
        )
        hits = res.get("hits", {}).get("hits", [])
        if hits:
            h = hits[0]
            return h.get("_id"), h.get("_source", {})
    except SAFE_EXCEPTIONS + (httpx.RequestError, httpx.HTTPStatusError):
        pass
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
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, context=cfg.ssl_ctx, timeout=_REQUEST_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 504) and attempt < _MAX_RETRIES - 1:
                time.sleep((2 ** attempt) * _BACKOFF_BASE_S)
                continue
            raise
        except SAFE_EXCEPTIONS:
            if attempt < _MAX_RETRIES - 1:
                time.sleep((2 ** attempt) * _BACKOFF_BASE_S)
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
        with urllib.request.urlopen(req, context=cfg.ssl_ctx, timeout=_REQUEST_TIMEOUT_S) as resp:
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
    with urllib.request.urlopen(req, context=cfg.ssl_ctx, timeout=_REQUEST_TIMEOUT_S) as resp:
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
    with urllib.request.urlopen(req, context=cfg.ssl_ctx, timeout=_REQUEST_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode())


def es_delete_doc(index: str, doc_id: str) -> bool:
    """Synchronous document delete.  Returns ``True`` on 404 (idempotent).

    Matches the async variant's semantics: a missing document is considered
    a successful delete (Finding 7 — consistent 404 handling).
    """
    cfg = _get_es_config()
    safe_id = urllib.parse.quote(str(doc_id), safe="")
    headers = _build_json_headers()
    req = urllib.request.Request(
        f"{cfg.url}/{index}/_doc/{safe_id}",
        headers=headers,
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, context=cfg.ssl_ctx, timeout=_REQUEST_TIMEOUT_S) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True  # idempotent — document already gone
        raise


def find_task_doc_by_logical_id(
    logical_id: str,
) -> Tuple[Optional[str], Optional[dict]]:
    """Synchronous task document lookup via a single ``bool.should`` query.

    Mirrors ``async_find_task_doc_by_logical_id`` — combines all lookup
    strategies into one ES round-trip (Finding 11).
    """
    tid = (logical_id or "").strip()
    if not tid:
        return None, None
    try:
        hits = (
            es_search(
                _TASK_RECORDS_INDEX,
                {
                    "size": 1,
                    "query": {
                        "bool": {
                            "should": [
                                {"ids": {"values": [tid]}},
                                {"term": {"id": tid}},
                                {"term": {"id.keyword": tid}},
                                {"match_phrase": {"id": tid}},
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                },
            )
            .get("hits", {})
            .get("hits", [])
        )
        if hits:
            h = hits[0]
            return h.get("_id"), h.get("_source", {})
    except SAFE_EXCEPTIONS:
        pass
    return None, None


# ── Bulk Buffering & Connection Resiliency (Findings 5, 12, 13) ─────────────


class BulkFlusher:
    """Encapsulated bulk-update buffer with event-driven flushing.

    Replaces the previous bare globals (``_ES_BULK_BUFFER``, ``_ES_BULK_LOCK``,
    ``_ES_BULK_LAST_FLUSH``) with a self-contained class that:

    - Uses ``threading.Event`` instead of a busy-wait spin loop (Finding 12).
    - Tracks dropped payloads via a counter for observability (Finding 13).
    - Makes flush thresholds configurable for unit testing (Finding 5).
    """

    def __init__(
        self,
        flush_threshold: int = _BULK_FLUSH_THRESHOLD,
        max_age_s: float = _BULK_MAX_AGE_S,
    ) -> None:
        self._buffer: list[dict] = []
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._last_flush = time.time()
        self._flush_threshold = flush_threshold
        self._max_age_s = max_age_s
        self._dropped_payloads: int = 0

    @property
    def dropped_payloads(self) -> int:
        """Total number of payloads dropped due to flush failures."""
        return self._dropped_payloads

    def enqueue(self, index: str, doc_id: str, doc: dict) -> None:
        """Thread-safe enqueue of a single update operation."""
        with self._lock:
            self._buffer.append({"index": index, "id": doc_id, "doc": doc})
        # Wake the flusher thread immediately if threshold is reached.
        self._event.set()

    def run_forever(self) -> None:
        """Background loop: block on event or timeout, then flush if needed.

        Uses ``threading.Event.wait(timeout=max_age_s)`` so the thread sleeps
        efficiently instead of polling every 50 ms (Finding 12).
        """
        while True:
            # Block until data arrives (.set()) or max_age_s elapses.
            self._event.wait(timeout=self._max_age_s)
            self._event.clear()
            with self._lock:
                now = time.time()
                if (
                    len(self._buffer) >= self._flush_threshold
                    or (
                        len(self._buffer) > 0
                        and (now - self._last_flush) >= self._max_age_s
                    )
                ):
                    self._flush_unlocked()

    def _flush_unlocked(self) -> None:
        """Flush all buffered updates to ES.  Caller MUST hold ``self._lock``."""
        if not self._buffer:
            return
        payloads = self._buffer
        self._buffer = []
        self._last_flush = time.time()

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
        for attempt in range(_MAX_RETRIES):
            try:
                with urllib.request.urlopen(
                    req, context=cfg.ssl_ctx, timeout=_BULK_FLUSH_TIMEOUT_S
                ):
                    pass
                return  # success
            except urllib.error.HTTPError as e:
                if e.code in (429, 503, 504) and attempt < _MAX_RETRIES - 1:
                    time.sleep((2 ** attempt) * _BACKOFF_BASE_S)
                    continue
                self._dropped_payloads += len(payloads)
                logger.warning(
                    "ES Bulk HTTP Flush failed",
                    extra={
                        "structured_data": {
                            "event": "es_bulk_http_flush_failed",
                            "error": str(e),
                            "attempt": attempt + 1,
                            "payloads_dropped": len(payloads),
                            "total_dropped": self._dropped_payloads,
                        }
                    },
                )
                return
            except SAFE_EXCEPTIONS as e:
                if attempt < _MAX_RETRIES - 1:
                    time.sleep((2 ** attempt) * _BACKOFF_BASE_S)
                    continue
                self._dropped_payloads += len(payloads)
                logger.warning(
                    "ES Bulk Connection failed",
                    extra={
                        "structured_data": {
                            "event": "es_bulk_connection_failed",
                            "error": str(e),
                            "attempt": attempt + 1,
                            "payloads_dropped": len(payloads),
                            "total_dropped": self._dropped_payloads,
                        }
                    },
                )
                return


# Module-level singleton — created once, started by server.py lifespan.
_bulk_flusher = BulkFlusher()


def _es_bulk_flusher_loop() -> None:
    """Entry point for the background bulk-flusher thread.

    Delegates to the ``BulkFlusher`` singleton.  Called by ``server.py``
    via ``threading.Thread(target=_es_bulk_flusher_loop, daemon=True).start()``.
    """
    _bulk_flusher.run_forever()


def es_bulk_update_proxy(path: str, body: dict, method: str = "POST") -> dict:
    """Intercept task-record updates and buffer them for bulk flushing.

    Non-matching requests are forwarded to ``es_post`` synchronously.
    """
    if method == "POST" and path.startswith(f"{_TASK_RECORDS_INDEX}/_update/"):
        doc_id = path.split("/")[-1]
        doc = body.get("doc", {})
        if doc:
            _bulk_flusher.enqueue(_TASK_RECORDS_INDEX, doc_id, doc)
            return {"status": "buffered"}
    return es_post(path, body, method)
