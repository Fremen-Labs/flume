#!/usr/bin/env python3
import json, os, ssl, urllib.request
from datetime import datetime, timezone

ES_URL = os.environ.get('ES_URL','https://localhost:9200').rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY')
ES_VERIFY_TLS = os.environ.get('ES_VERIFY_TLS','false').lower() == 'true'
INDEX = os.environ.get('ES_INDEX_TASKS','agent-task-records')
if not ES_API_KEY:
    raise SystemExit('ES_API_KEY is required')
ctx=None
if not ES_VERIFY_TLS:
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
headers={'Content-Type':'application/json','Authorization':f'ApiKey {ES_API_KEY}'}
search_body={'size':500,'query':{'match_all':{}},'sort':[{'updated_at':{'order':'desc','unmapped_type':'date'}}]}
req=urllib.request.Request(f"{ES_URL}/{INDEX}/_search", data=json.dumps(search_body).encode(), headers=headers, method='GET')
with urllib.request.urlopen(req, context=ctx) as resp:
    hits=json.loads(resp.read().decode()).get('hits',{}).get('hits',[])
items=[{'_id':h['_id'], **h.get('_source',{})} for h in hits]
by_id={i.get('id', i.get('_id')): i for i in items}
children={}
for i in items:
    pid=i.get('parent_id')
    if pid:
        children.setdefault(pid, []).append(i)
now=datetime.now(timezone.utc).isoformat()
changes=[]
for i in items:
    item_id=i.get('id', i.get('_id'))
    deps=i.get('depends_on') or []
    has_children=bool(children.get(item_id))
    if has_children:
        continue
    if i.get('status') in ('done','running','blocked','review','failed','archived'):
        continue
    ready=all(by_id.get(d,{}).get('status')=='done' for d in deps)
    new_status='ready' if ready else 'planned'
    if i.get('status') != new_status:
        body={'doc':{'status':new_status,'updated_at':now,'last_update':now}}
        ureq=urllib.request.Request(f"{ES_URL}/{INDEX}/_update/{i['_id']}", data=json.dumps(body).encode(), headers=headers, method='POST')
        with urllib.request.urlopen(ureq, context=ctx) as r:
            changes.append({'id': item_id, 'status': new_status})
print(json.dumps({'updated': changes}, indent=2))
