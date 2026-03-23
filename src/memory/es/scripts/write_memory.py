#!/usr/bin/env python3
import json
import os
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent.parent / 'worker-manager'
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


def write_memory_entry(entry_id, title, content, entry_type, project, repo, scope=None, confidence=0.5, status="active", tags=None, source="agent"):
    """
    Write a memory entry to Elasticsearch.
    
    :param entry_id: Unique identifier for the memory entry
    :param title: Title of the memory entry
    :param content: Content/body of the memory entry
    :param entry_type: Type of memory entry (decision, lesson, constraint, etc.)
    :param project: Project this entry belongs to
    :param repo: Repository this entry belongs to
    :param scope: Scope of the entry (optional)
    :param confidence: Confidence level (0.0-1.0)
    :param status: Status of the entry (active, deprecated, etc.)
    :param tags: List of tags for categorization
    :param source: Source of the entry (agent, manual, etc.)
    """
    doc = {
        'id': entry_id,
        'title': title,
        'content': content,
        'type': entry_type,
        'project': project,
        'repo': repo,
        'scope': scope,
        'confidence': confidence,
        'status': status,
        'source': source,
        'created_at': now_iso(),
        'updated_at': now_iso(),
        'tags': tags or []
    }
    
    try:
        es_request(f'/{MEMORY_INDEX}/_doc/{entry_id}', doc, method='POST')
        log(f"Memory entry '{title}' written successfully")
        return True
    except Exception as e:
        log(f"Failed to write memory entry '{title}': {e}")
        return False


def main():
    # This is a placeholder for the actual implementation
    # In practice, this script would be called with specific parameters
    print("Memory writing script initialized")


if __name__ == '__main__':
    main()