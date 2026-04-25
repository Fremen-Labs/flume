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
project, repo, root_title, spec_file = sys.argv[1:5]
spec = json.loads(open(spec_file).read())
now = datetime.now(timezone.utc).isoformat()
ctx=None
if not ES_VERIFY_TLS:
    ctx=ssl.create_default_context()
    ctx.check_hostname=False
    ctx.verify_mode=ssl.CERT_NONE
headers={'Content-Type':'application/json','Authorization':f'ApiKey {ES_API_KEY}'}
created=[]

def infer_model(node_type, role=None):
    if role == 'reviewer':
        return 'codex-code-review'
    if role in ('intake','pm','dispatcher','memory-updater'):
        return 'gpt-codex'
    if node_type in ('epic','feature','story'):
        return 'gpt-codex'
    return 'qwen3'

def put(doc):
    doc.setdefault('project', project)
    doc.setdefault('repo', repo)
    doc.setdefault('created_at', now)
    doc.setdefault('updated_at', now)
    doc.setdefault('last_update', now)
    doc.setdefault('acceptance_criteria', [])
    doc.setdefault('artifacts', [])
    doc.setdefault('needs_human', False)
    doc.setdefault('risk', 'medium')
    doc.setdefault('depends_on', [])
    doc.setdefault('preferred_model', infer_model(doc.get('work_item_type'), doc.get('assigned_agent_role')))
    req=urllib.request.Request(f"{ES_URL}/{INDEX}/_doc/{doc['id']}", data=json.dumps(doc).encode(), headers=headers, method='PUT')
    with urllib.request.urlopen(req, context=ctx) as resp:
        created.append(json.loads(resp.read().decode()))

def walk(node, parent_id=None):
    node_type = node['type']
    role = node.get('assigned_agent_role')
    if not role:
        if node_type in ('epic','feature','story'):
            role = 'pm'
        elif node_type == 'bug':
            role = 'implementer'
        else:
            role = 'implementer'
    doc = {
        'id': node['id'],
        'title': node['title'],
        'work_item_type': node_type,
        'parent_id': parent_id,
        'status': node.get('status', 'planned' if node.get('children') else 'ready'),
        'priority': node.get('priority', 'normal'),
        'assigned_agent_role': role,
        'execution_host': node.get('execution_host'),
        'objective': node.get('objective', node['title']),
        'preferred_model': node.get('preferred_model', infer_model(node_type, role))
    }
    if 'depends_on' in node:
        doc['depends_on'] = node['depends_on']
    put(doc)
    for child in node.get('children', []):
        walk(child, node['id'])

root = spec
if root.get('title') != root_title:
    root['title'] = root_title
walk(root)
print(json.dumps({'created_count': len(created), 'root_id': root['id']}, indent=2))
