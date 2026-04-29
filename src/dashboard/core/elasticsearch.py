"""Core REST adapter for Elasticsearch interactions (sync/async) and bulk buffering."""
import json
import ssl
import threading
import time
import base64
import urllib.error
import urllib.parse
import urllib.request
import httpx
import asyncio
from typing import Optional, Tuple

from utils.logger import get_logger
from utils.exceptions import SAFE_EXCEPTIONS

logger = get_logger(__name__)

# --- Configuration defaults (single source of truth for ES connectivity) ---
from config import get_settings  # noqa: E402
_settings = get_settings()

if _settings.FLUME_NATIVE_MODE == '1':
    # Standalone processes can reach ES via localhost loopback normally patched via Node registries
    _DEFAULT_ES = "http://localhost:9200"
else:
    # Dockerized environments must route natively
    _DEFAULT_ES = "http://elasticsearch:9200"

ES_URL = _settings.ES_URL.rstrip('/') if _settings.ES_URL else _DEFAULT_ES
ES_API_KEY = _settings.ES_API_KEY
ES_PASSWORD = _settings.FLUME_ELASTIC_PASSWORD
ES_VERIFY_TLS = _settings.ES_VERIFY_TLS

def _get_auth_headers() -> dict:
    if ES_API_KEY:
        return {'Authorization': f'ApiKey {ES_API_KEY}'}
    if ES_PASSWORD:
        b64 = base64.b64encode(f"elastic:{ES_PASSWORD}".encode()).decode()
        return {'Authorization': f'Basic {b64}'}
    return {}

# SSL context — respects ES_VERIFY_TLS env var to gate certificate validation.
# Default: TLS verification OFF (self-signed ES clusters common in dev).
# Set ES_VERIFY_TLS=true in production to enforce certificate validation.
ctx = None
if ES_URL.startswith("https:"):
    ctx = ssl.create_default_context()
    if not ES_VERIFY_TLS:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # else: default context verifies certs and hostnames

def _get_httpx_verify() -> bool | ssl.SSLContext:
    if ctx is not None:
        if not ES_VERIFY_TLS:
            return False
        return ctx
    return True

# --- ES Utility Functions ---

def es_search(index: str, body: dict) -> dict:
    headers = {'Content-Type': 'application/json'}
    headers.update(_get_auth_headers())
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_search",
        data=json.dumps(body).encode(),
        headers=headers,
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise

async def async_es_delete_doc(index: str, doc_id: str) -> bool:
    safe_id = urllib.parse.quote(str(doc_id), safe='')
    headers = {'Content-Type': 'application/json'}
    headers.update(_get_auth_headers())
    async with httpx.AsyncClient(verify=_get_httpx_verify()) as client:
        try:
            resp = await client.delete(
                f"{ES_URL}/{index}/_doc/{safe_id}",
                headers=headers
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return True
            raise

async def async_es_search(index: str, body: dict) -> dict:
    headers = {'Content-Type': 'application/json'}
    headers.update(_get_auth_headers())
    async with httpx.AsyncClient(verify=_get_httpx_verify()) as client:
        resp = await client.post(
            f"{ES_URL}/{index}/_search",
            json=body,
            headers=headers
        )
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json()


def find_task_doc_by_logical_id(logical_id: str) -> Tuple[Optional[str], Optional[dict]]:
    tid = (logical_id or '').strip()
    if not tid:
        return None, None
    attempts = [
        {'ids': {'values': [tid]}},
        {'term': {'id': tid}},
        {'term': {'id.keyword': tid}},
        {'match_phrase': {'id': tid}},
    ]
    for query in attempts:
        try:
            hits = es_search('agent-task-records', {'size': 1, 'query': query}).get('hits', {}).get('hits', [])
            if hits:
                h = hits[0]
                return h.get('_id'), h.get('_source', {})
        except SAFE_EXCEPTIONS:
            continue
    return None, None

async def async_find_task_doc_by_logical_id(logical_id: str) -> Tuple[Optional[str], Optional[dict]]:
    tid = (logical_id or '').strip()
    if not tid:
        return None, None
    attempts = [
        {'ids': {'values': [tid]}},
        {'term': {'id': tid}},
        {'term': {'id.keyword': tid}},
        {'match_phrase': {'id': tid}},
    ]
    for query in attempts:
        try:
            res = await async_es_search('agent-task-records', {'size': 1, 'query': query})
            hits = res.get('hits', {}).get('hits', [])
            if hits:
                h = hits[0]
                return h.get('_id'), h.get('_source', {})
        except SAFE_EXCEPTIONS + (httpx.RequestError, httpx.HTTPStatusError):
            continue
    return None, None

def es_index(index: str, doc: dict) -> dict:
    headers = {'Content-Type': 'application/json'}
    headers.update(_get_auth_headers())
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_doc",
        data=json.dumps(doc).encode(),
        headers=headers,
        method='POST',
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode())

async def async_es_index(index: str, doc: dict) -> dict:
    headers = {'Content-Type': 'application/json'}
    headers.update(_get_auth_headers())
    async with httpx.AsyncClient(verify=_get_httpx_verify()) as client:
        resp = await client.post(
            f"{ES_URL}/{index}/_doc",
            json=doc,
            headers=headers
        )
        resp.raise_for_status()
        return resp.json()


def es_upsert(index: str, doc_id: str, doc: dict) -> dict:
    headers = {'Content-Type': 'application/json'}
    headers.update(_get_auth_headers())
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_doc/{urllib.parse.quote(str(doc_id), safe='')}",
        data=json.dumps(doc).encode(),
        headers=headers,
        method='PUT',
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode())

async def async_es_upsert(index: str, doc_id: str, doc: dict) -> dict:
    headers = {'Content-Type': 'application/json'}
    headers.update(_get_auth_headers())
    async with httpx.AsyncClient(verify=_get_httpx_verify()) as client:
        resp = await client.put(
            f"{ES_URL}/{index}/_doc/{urllib.parse.quote(str(doc_id), safe='')}",
            json=doc,
            headers=headers
        )
        resp.raise_for_status()
        return resp.json()


# --- ES Bulk Buffering & Connection Resiliency ---

_ES_BULK_BUFFER = []
_ES_BULK_LOCK = threading.Lock()
_ES_BULK_LAST_FLUSH = time.time()

def _es_bulk_flusher_loop():
    global _ES_BULK_LAST_FLUSH
    while True:
        time.sleep(0.05)
        with _ES_BULK_LOCK:
            now = time.time()
            if len(_ES_BULK_BUFFER) >= 50 or (len(_ES_BULK_BUFFER) > 0 and (now - _ES_BULK_LAST_FLUSH) >= 0.25):
                _flush_es_bulk_unlocked()

def _flush_es_bulk_unlocked():
    global _ES_BULK_BUFFER, _ES_BULK_LAST_FLUSH
    if not _ES_BULK_BUFFER:
        return
    payloads = _ES_BULK_BUFFER
    _ES_BULK_BUFFER = []
    _ES_BULK_LAST_FLUSH = time.time()
    
    ndjson = ""
    for op in payloads:
        ndjson += json.dumps({'update': {'_index': op['index'], '_id': op['id']}}) + "\n"
        ndjson += json.dumps({'doc': op['doc']}) + "\n"
    ndjson += "\n"
    
    headers = {'Content-Type': 'application/x-ndjson'}
    headers.update(_get_auth_headers())
    req = urllib.request.Request(
        f"{ES_URL}/_bulk",
        data=ndjson.encode('utf-8'),
        headers=headers,
        method='POST',
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15):
                pass
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 504) and attempt < 3:
                time.sleep((2 ** attempt) * 0.1)
                continue
            logger.warning(
                "ES Bulk HTTP Flush failed",
                extra={"structured_data": {"event": "es_bulk_http_flush_failed", "error": str(e)}}
            )
            break
        except SAFE_EXCEPTIONS as e:
            if attempt < 3:
                time.sleep((2 ** attempt) * 0.1)
                continue
            logger.warning(
                "ES Bulk Connection failed",
                extra={"structured_data": {"event": "es_bulk_connection_failed", "error": str(e)}}
            )
            break

def es_bulk_update_proxy(path: str, body: dict, method: str = 'POST') -> dict:
    if method == 'POST' and path.startswith('agent-task-records/_update/'):
        doc_id = path.split('/')[-1]
        doc = body.get('doc', {})
        if doc:
            with _ES_BULK_LOCK:
                _ES_BULK_BUFFER.append({'index': 'agent-task-records', 'id': doc_id, 'doc': doc})
            return {'status': 'buffered'}
    return es_post(path, body, method)

def es_post(path: str, body: dict, method: str = 'POST') -> dict:
    headers = {'Content-Type': 'application/json'}
    headers.update(_get_auth_headers())
    req = urllib.request.Request(
        f"{ES_URL}/{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method=method,
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
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

async def async_es_post(path: str, body: dict, method: str = 'POST') -> dict:
    headers = {'Content-Type': 'application/json'}
    headers.update(_get_auth_headers())
    async with httpx.AsyncClient(verify=_get_httpx_verify()) as client:
        for attempt in range(4):
            try:
                resp = await client.request(
                    method=method,
                    url=f"{ES_URL}/{path}",
                    json=body,
                    headers=headers,
                    timeout=10.0
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

def es_delete_doc(index: str, doc_id: str) -> bool:
    safe_id = urllib.parse.quote(str(doc_id), safe='')
    headers = {'Content-Type': 'application/json'}
    headers.update(_get_auth_headers())
    req = urllib.request.Request(
        f'{ES_URL}/{index}/_doc/{safe_id}',
        headers=headers,
        method='DELETE',
    )
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
