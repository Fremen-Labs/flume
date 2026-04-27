#!/usr/bin/env python3
import json
import os
import sys
import ssl
import urllib.request
from datetime import datetime, timezone

ES_URL = os.environ.get('ES_URL','https://localhost:9200').rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY')
ES_VERIFY_TLS = os.environ.get('ES_VERIFY_TLS','false').lower() == 'true'
INDEX = os.environ.get('ES_INDEX_TASKS','agent-task-records')
if not ES_API_KEY:
    raise SystemExit('ES_API_KEY is required')
if len(sys.argv) < 5:
    raise SystemExit('usage: generate_work_tree.py <project> <repo> <root_title> <json_spec_file>')
payload = json.load(sys.stdin)
now = datetime.now(timezone.utc).isoformat()
payload.setdefault('created_at', now)
payload['updated_at'] = now
payload.setdefault('last_update', now)
payload.setdefault('status', 'inbox')
payload.setdefault('work_item_type', 'task')
payload.setdefault('parent_id', None)
payload.setdefault('depends_on', [])
payload.setdefault('assigned_agent_role', None)
payload.setdefault('execution_host', None)
payload.setdefault('preferred_model', None)
doc_id = payload.get('id')
if not doc_id:
    raise SystemExit('id required')
ctx = None
if not ES_VERIFY_TLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname=False
    ctx.verify_mode=ssl.CERT_NONE
req = urllib.request.Request(f"{ES_URL}/{INDEX}/_doc/{doc_id}", data=json.dumps(payload).encode(), headers={'Content-Type':'application/json','Authorization':f'ApiKey {ES_API_KEY}'}, method='PUT')
with urllib.request.urlopen(req, context=ctx) as resp:
    print(resp.read().decode())
