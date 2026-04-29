"""Elasticsearch-backed ID sequence generation and high-water mark counters."""
from datetime import datetime, timezone
import json
import re
import urllib.request
import urllib.error
from utils.exceptions import SAFE_EXCEPTIONS

from utils.logger import get_logger
from core.elasticsearch import es_search, get_es_url, get_ssl_context, _get_auth_headers

logger = get_logger(__name__)
COUNTERS_INDEX = 'flume-counters'

def _es_counter_request(path: str, body=None, method: str = 'GET') -> dict:
    """Thin HTTP helper scoped to the flume-counters ES index."""
    headers = {'Content-Type': 'application/json'}
    headers.update(_get_auth_headers())
    data = json.dumps(body).encode() if body is not None else None
    if data and method == 'GET':
        method = 'POST'
    req = urllib.request.Request(f'{get_es_url()}{path}', data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=get_ssl_context()) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise

def es_counter_hwm(prefix: str) -> int:
    """
    Return the stored high-water-mark for *prefix* from the ES flume-counters index.
    Returns 0 if the document does not yet exist or ES is unreachable.
    """
    try:
        res = _es_counter_request(f'/{COUNTERS_INDEX}/_doc/{prefix}')
        return int(res.get('_source', {}).get('value', 0))
    except SAFE_EXCEPTIONS:
        return 0

def es_counter_set_hwm(prefix: str, value: int) -> None:
    """
    Atomically raise the stored counter for *prefix* to *value* if it is higher.
    Uses a Painless script so concurrent dashboard replicas are safe.
    """
    if value <= 0:
        return
    now = datetime.now(timezone.utc).isoformat()
    body = {
        'scripted_upsert': True,
        'script': {
            'source': (
                'if (ctx._source.containsKey("value")) {'
                '  ctx._source.value = Math.max(ctx._source.value, (long)params.v);'
                '} else {'
                '  ctx._source.value = (long)params.v;'
                '}'
                ' ctx._source.updated_at = params.ts;'
                ' ctx._source.prefix = params.pfx;'
            ),
            'lang': 'painless',
            'params': {'v': value, 'ts': now, 'pfx': prefix},
        },
        'upsert': {'prefix': prefix, 'value': value, 'updated_at': now},
    }
    try:
        _es_counter_request(f'/{COUNTERS_INDEX}/_update/{prefix}', body=body, method='POST')
    except SAFE_EXCEPTIONS as exc:
        logger.warning(
            "Failed to set counter high-water mark",
            extra={
                "structured_data": {
                    "event": "es_counter_set_hwm_failed",
                    "prefix": prefix,
                    "value": value,
                    "error": str(exc),
                }
            }
        )

def get_next_id_sequence(prefix: str) -> int:
    """
    Return the next available integer sequence number for IDs of the form `prefix-N`.

    Takes the maximum of:
      1. The highest N seen in the live ES index (covers active/archived records).
      2. The persisted high-water-mark counter (covers IDs that were deleted from ES).

    This guarantees monotonic, never-recycled IDs even when records are hard-deleted.
    """
    max_n = es_counter_hwm(prefix)
    try:
        hits = es_search('agent-task-records', {
            'size': 10000,
            '_source': ['id'],
            'query': {'regexp': {'id': f'{re.escape(prefix)}-[0-9]+'}}
        }).get('hits', {}).get('hits', [])
        pattern = re.compile(rf'^{re.escape(prefix)}-(\d+)$')
        for h in hits:
            doc_id = (h.get('_source') or {}).get('id', '') or h.get('_id', '')
            m = pattern.match(doc_id)
            if m:
                max_n = max(max_n, int(m.group(1)))
    except SAFE_EXCEPTIONS:
        if max_n == 0:
            return int(datetime.now(timezone.utc).timestamp()) % 1_000_000 + 1
    return max_n + 1
