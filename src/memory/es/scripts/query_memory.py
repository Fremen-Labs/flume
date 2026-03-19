#!/usr/bin/env python3
import json
import os
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(os.environ.get('LOOM_WORKSPACE', str(Path(__file__).parent.parent.parent.parent))) / 'worker-manager'
LOG = BASE / 'memory_updater.log'

ES_URL = os.environ.get('ES_URL', 'https://localhost:9200').rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY', '')
ES_VERIFY_TLS = os.environ.get('ES_VERIFY_TLS', 'false').lower() == 'true'
MEMORY_INDEX = os.environ.get('ES_INDEX_MEMORY', 'agent-memory-entries')

ctx = None
if not ES_VERIFY_TLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def log(msg):
    BASE.mkdir(parents=True, exist_ok=True)
    with LOG.open('a') as f:
        f.write(f"[{now_iso()}] {msg}\n")


def es_request(path, body=None, method='GET'):
    headers = {'Authorization': f'ApiKey {ES_API_KEY}'}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"{ES_URL}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, context=ctx) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def query_memory(query, project=None, repo=None, entry_type=None, limit=10):
    """
    Query memory entries from Elasticsearch.
    
    :param query: Search query string
    :param project: Filter by project (optional)
    :param repo: Filter by repository (optional)
    :param entry_type: Filter by entry type (optional)
    :param limit: Maximum number of results to return
    :return: List of matching memory entries
    """
    # Build the query
    must_clauses = []
    
    # Add text search
    if query:
        must_clauses.append({
            "multi_match": {
                "query": query,
                "type": "best_fields",
                "fields": ["title^2", "content"],
                "tie_breaker": 0.3
            }
        })
    
    # Add project filter
    if project:
        must_clauses.append({"term": {"project": project}})
    
    # Add repo filter
    if repo:
        must_clauses.append({"term": {"repo": repo}})
    
    # Add type filter
    if entry_type:
        must_clauses.append({"term": {"type": entry_type}})
    
    # Build the full query
    body = {
        "size": limit,
        "query": {
            "bool": {
                "must": must_clauses
            }
        },
        "sort": [
            {"confidence": {"order": "desc"}},
            {"created_at": {"order": "desc"}}
        ]
    }
    
    try:
        response = es_request(f'/{MEMORY_INDEX}/_search', body, method='GET')
        hits = response.get('hits', {}).get('hits', [])
        
        results = []
        for hit in hits:
            source = hit.get('_source', {})
            results.append({
                'id': source.get('id'),
                'title': source.get('title'),
                'content': source.get('content'),
                'type': source.get('type'),
                'project': source.get('project'),
                'repo': source.get('repo'),
                'confidence': source.get('confidence'),
                'created_at': source.get('created_at'),
                'tags': source.get('tags', [])
            })
        
        log(f"Memory query for '{query}' returned {len(results)} results")
        return results
    except Exception as e:
        log(f"Failed to query memory: {e}")
        return []


def main():
    # This is a placeholder for the actual implementation
    # In practice, this script would be called with specific parameters
    print("Memory querying script initialized")


if __name__ == '__main__':
    main()