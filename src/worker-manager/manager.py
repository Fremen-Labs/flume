#!/usr/bin/env python3
import json
import os
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(os.environ.get('LOOM_WORKSPACE', str(Path(__file__).parent.parent))) / 'worker-manager'
STATE = BASE / 'state.json'
LOG = BASE / 'manager.log'

ES_URL = os.environ.get('ES_URL', 'https://localhost:9200').rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY', '')
ES_VERIFY_TLS = os.environ.get('ES_VERIFY_TLS', 'false').lower() == 'true'
TASK_INDEX = os.environ.get('ES_INDEX_TASKS', 'agent-task-records')
POLL_SECONDS = int(os.environ.get('WORKER_MANAGER_POLL_SECONDS', '15'))
WORKERS_PER_ROLE = int(os.environ.get('WORKERS_PER_ROLE', '1'))

ROLE_DEFS = [
    {'role': 'intake',         'model': os.environ.get('LLM_MODEL', 'llama3.2'), 'execution_host': os.environ.get('EXECUTION_HOST', 'localhost')},
    {'role': 'pm',             'model': os.environ.get('LLM_MODEL', 'llama3.2'), 'execution_host': os.environ.get('EXECUTION_HOST', 'localhost')},
    {'role': 'implementer',    'model': os.environ.get('LLM_MODEL', 'llama3.2'), 'execution_host': os.environ.get('EXECUTION_HOST', 'localhost')},
    {'role': 'tester',         'model': os.environ.get('LLM_MODEL', 'llama3.2'), 'execution_host': os.environ.get('EXECUTION_HOST', 'localhost')},
    {'role': 'reviewer',       'model': os.environ.get('LLM_MODEL', 'llama3.2'), 'execution_host': os.environ.get('EXECUTION_HOST', 'localhost')},
    {'role': 'memory-updater', 'model': os.environ.get('LLM_MODEL', 'llama3.2'), 'execution_host': os.environ.get('EXECUTION_HOST', 'localhost')},
]


def build_workers():
    workers = []
    for role_def in ROLE_DEFS:
        for idx in range(1, max(WORKERS_PER_ROLE, 1) + 1):
            workers.append({
                'name': f"{role_def['role']}-worker-{idx}",
                'role': role_def['role'],
                'model': role_def['model'],
                'execution_host': role_def['execution_host'],
            })
    return workers


WORKERS = build_workers()

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


def ready_items_for_role(role):
    must = []
    if role == 'implementer':
        must = [
            {'term': {'status': 'ready'}},
            {'bool': {'should': [
                {'term': {'assigned_agent_role': 'implementer'}},
                {'term': {'owner': 'implementer'}},
            ], 'minimum_should_match': 1}},
        ]
    elif role == 'tester':
        must = [
            {'term': {'status': 'review'}},
            {'bool': {'should': [
                {'term': {'assigned_agent_role': 'tester'}},
                {'term': {'owner': 'tester'}},
            ], 'minimum_should_match': 1}},
        ]
    elif role == 'reviewer':
        must = [
            {'term': {'status': 'review'}},
            {'bool': {'should': [
                {'term': {'assigned_agent_role': 'reviewer'}},
                {'term': {'owner': 'reviewer'}},
            ], 'minimum_should_match': 1}},
        ]
    elif role == 'pm':
        # PM should only claim items that are actually owned/assigned to PM
        # (epics/features/stories). Tasks inside stories must not be
        # accidentally re-owned by PM.
        must = [
            {'term': {'status': 'planned'}},
            {'bool': {
                'should': [
                    {'term': {'owner': 'pm'}},
                    {'term': {'assigned_agent_role': 'pm'}},
                ],
                'minimum_should_match': 1,
            }},
        ]
    elif role in ('pm', 'intake', 'memory-updater'):
        must = [
            {'term': {'status': 'ready'}},
            {'term': {'assigned_agent_role': role}},
        ]
    body = {
        'size': 20,
        'query': {'bool': {'must': must}},
        'sort': [
            {'updated_at': {'order': 'asc', 'unmapped_type': 'date'}}
        ]
    }
    return es_request(f'/{TASK_INDEX}/_search', body, method='GET').get('hits', {}).get('hits', [])


def claim(item_id, role, execution_host=None, preferred_model=None, worker_name=None):
    doc = {
        'status': 'running' if role != 'pm' else 'planned',
        'queue_state': 'active',
        'assigned_agent_role': role,
        'owner': role,
        'updated_at': now_iso(),
        'last_update': now_iso(),
    }
    if execution_host:
        doc['execution_host'] = execution_host
    if preferred_model:
        doc['preferred_model'] = preferred_model
    if worker_name:
        doc['active_worker'] = worker_name
    es_request(f'/{TASK_INDEX}/_update/{item_id}', {'doc': doc}, method='POST')


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {'workers': []}


def save_state(state):
    BASE.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + '\n')


def cycle():
    state = {'updated_at': now_iso(), 'workers': []}
    for worker in WORKERS:
        snapshot = dict(worker)
        snapshot['heartbeat_at'] = now_iso()
        snapshot['status'] = 'idle'
        snapshot['current_task_id'] = None
        hits = ready_items_for_role(worker['role'])
        if hits:
            task = hits[0]
            item_id = task.get('_id')
            src = task.get('_source', {})
            claim(item_id, worker['role'], worker.get('execution_host'), src.get('preferred_model') or worker['model'], worker['name'])
            snapshot['status'] = 'claimed'
            snapshot['current_task_id'] = src.get('id', item_id)
            snapshot['current_task_title'] = src.get('title')
            snapshot['preferred_model'] = src.get('preferred_model') or worker['model']
            log(f"{worker['name']} claimed {snapshot['current_task_id']}")
        state['workers'].append(snapshot)
    save_state(state)


def main():
    if not ES_API_KEY:
        raise SystemExit('ES_API_KEY is required')
    log('worker manager starting')
    while True:
        try:
            cycle()
        except Exception as e:
            log(f'cycle error: {e}')
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
