import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)

# --- Configuration defaults ---
_DEFAULT_ES = "http://localhost:9200"
if os.environ.get('FLUME_NATIVE_MODE') == '1':
    # Standalone processes can reach ES via localhost loopback normally patched via Node registries
    _DEFAULT_ES = "http://localhost:9200"
else:
    # Dockerized environments must route natively
    _DEFAULT_ES = "http://elasticsearch:9200"

ES_URL = os.environ.get('ES_URL', _DEFAULT_ES).rstrip('/')

# Provide SSL context (can be overridden by setup routines avoiding insecure cert warnings natively)
ctx = None
if ES_URL.startswith("https:"):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

# --- ES Utility Functions ---

def es_search(index: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_search",
        data=json.dumps(body).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise

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
        except Exception:
            continue
    return None, None

def es_index(index: str, doc: dict) -> dict:
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_doc",
        data=json.dumps(doc).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode())

def es_upsert(index: str, doc_id: str, doc: dict) -> dict:
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_doc/{urllib.parse.quote(str(doc_id), safe='')}",
        data=json.dumps(doc).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
        method='PUT',
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode())

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
    
    req = urllib.request.Request(
        f"{ES_URL}/_bulk",
        data=ndjson.encode('utf-8'),
        headers={
            'Content-Type': 'application/x-ndjson',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
        method='POST',
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                pass
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 504) and attempt < 3:
                time.sleep((2 ** attempt) * 0.1)
                continue
            logger.warning(f"[ES BULK] HTTP Flush failed: {e}")
            break
        except Exception as e:
            if attempt < 3:
                time.sleep((2 ** attempt) * 0.1)
                continue
            logger.warning(f"[ES BULK] Connection failed: {e}")
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
    req = urllib.request.Request(
        f"{ES_URL}/{path}",
        data=json.dumps(body).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
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
        except Exception as e:
            if attempt < 3:
                time.sleep((2 ** attempt) * 0.1)
                continue
            raise

def es_delete_doc(index: str, doc_id: str) -> bool:
    safe_id = urllib.parse.quote(str(doc_id), safe='')
    req = urllib.request.Request(
        f'{ES_URL}/{index}/_doc/{safe_id}',
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
        method='DELETE',
    )
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
