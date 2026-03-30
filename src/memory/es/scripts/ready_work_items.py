#!/usr/bin/env python3
import json
import os
import ssl
import urllib.request
ES_URL = os.environ.get('ES_URL','https://localhost:9200').rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY')
ES_VERIFY_TLS = os.environ.get('ES_VERIFY_TLS','false').lower() == 'true'
INDEX = os.environ.get('ES_INDEX_TASKS','agent-task-records')
if not ES_API_KEY:
    raise SystemExit('ES_API_KEY is required')
ctx=None
if not ES_VERIFY_TLS:
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
body={
  'size': 100,
  'query': {'bool': {'must': [{'term': {'status': 'ready'}}]}},
  'sort': [
    {'priority': {'order': 'desc', 'unmapped_type': 'keyword'}},
    {'updated_at': {'order': 'asc', 'unmapped_type': 'date'}}
  ]
}
req=urllib.request.Request(f"{ES_URL}/{INDEX}/_search", data=json.dumps(body).encode(), headers={'Content-Type':'application/json','Authorization':f'ApiKey {ES_API_KEY}'}, method='GET')
with urllib.request.urlopen(req, context=ctx) as resp:
    print(resp.read().decode())
