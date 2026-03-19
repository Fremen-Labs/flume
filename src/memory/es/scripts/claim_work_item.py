#!/usr/bin/env python3
import json, os, sys, ssl, urllib.request
from datetime import datetime, timezone
ES_URL = os.environ.get('ES_URL','https://localhost:9200').rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY')
ES_VERIFY_TLS = os.environ.get('ES_VERIFY_TLS','false').lower() == 'true'
INDEX = os.environ.get('ES_INDEX_TASKS','agent-task-records')
if not ES_API_KEY:
    raise SystemExit('ES_API_KEY is required')
if len(sys.argv) < 3:
    raise SystemExit('usage: claim_work_item.py <id> <agent_role> [execution_host]')
item_id, role = sys.argv[1:3]
execution_host = sys.argv[3] if len(sys.argv) > 3 else None
now = datetime.now(timezone.utc).isoformat()
ctx=None
if not ES_VERIFY_TLS:
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
headers={'Content-Type':'application/json','Authorization':f'ApiKey {ES_API_KEY}'}
get_req=urllib.request.Request(f"{ES_URL}/{INDEX}/_doc/{item_id}", headers=headers, method='GET')
with urllib.request.urlopen(get_req, context=ctx) as resp:
    current=json.loads(resp.read().decode()).get('_source',{})
preferred_model=current.get('preferred_model')
body={'doc':{'status':'running','queue_state':'active','assigned_agent_role':role,'owner':role,'updated_at':now,'last_update':now,'preferred_model':preferred_model}}
if execution_host:
    body['doc']['execution_host']=execution_host
req=urllib.request.Request(f"{ES_URL}/{INDEX}/_update/{item_id}", data=json.dumps(body).encode(), headers=headers, method='POST')
with urllib.request.urlopen(req, context=ctx) as resp:
    print(resp.read().decode())
