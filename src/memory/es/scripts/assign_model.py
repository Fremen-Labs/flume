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
if len(sys.argv) < 3:
    raise SystemExit('usage: assign_model.py <id> <preferred_model>')
item_id, preferred_model = sys.argv[1:3]
now = datetime.now(timezone.utc).isoformat()
ctx=None
if not ES_VERIFY_TLS:
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
headers={'Content-Type':'application/json','Authorization':f'ApiKey {ES_API_KEY}'}
body={'doc':{'preferred_model':preferred_model,'updated_at':now,'last_update':now}}
req=urllib.request.Request(f"{ES_URL}/{INDEX}/_update/{item_id}", data=json.dumps(body).encode(), headers=headers, method='POST')
with urllib.request.urlopen(req, context=ctx) as resp:
    print(resp.read().decode())
