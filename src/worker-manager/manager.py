#!/usr/bin/env python3
import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_WS = Path(os.environ.get('LOOM_WORKSPACE', str(Path(__file__).parent.parent)))
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))
from flume_secrets import apply_runtime_config  # noqa: E402
from workspace_llm_env import resolve_cloud_agent_model, sync_llm_env_from_workspace  # noqa: E402

apply_runtime_config(_WS)

BASE = _WS / 'worker-manager'
STATE = BASE / 'state.json'
AGENT_MODELS_FILE = BASE / 'agent_models.json'
LOG = BASE / 'manager.log'

ES_URL = os.environ.get('ES_URL', 'https://localhost:9200').rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY', '')
ES_VERIFY_TLS = os.environ.get('ES_VERIFY_TLS', 'false').lower() == 'true'
TASK_INDEX = os.environ.get('ES_INDEX_TASKS', 'agent-task-records')
POLL_SECONDS = int(os.environ.get('WORKER_MANAGER_POLL_SECONDS', '15'))
WORKERS_PER_ROLE = int(os.environ.get('WORKERS_PER_ROLE', '1'))

ROLE_ORDER = [
    'intake',
    'pm',
    'implementer',
    'tester',
    'reviewer',
    'memory-updater',
]

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


def load_agent_role_defs():
    """Merge per-role overrides from agent_models.json with LLM_* / EXECUTION_HOST env."""
    default_model = (os.environ.get('LLM_MODEL') or 'llama3.2').strip() or 'llama3.2'
    default_host = (os.environ.get('EXECUTION_HOST') or 'localhost').strip() or 'localhost'
    default_prov = os.environ.get('LLM_PROVIDER', 'ollama').strip().lower()
    cfg = {}
    if AGENT_MODELS_FILE.is_file():
        try:
            data = json.loads(AGENT_MODELS_FILE.read_text(encoding='utf-8'))
            cfg = (data.get('roles') or {}) if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            pass
    defs = []
    for role in ROLE_ORDER:
        spec = cfg.get(role)
        model = default_model
        host = default_host
        prov = default_prov
        if isinstance(spec, str):
            model = spec.strip() or model
        elif isinstance(spec, dict):
            model = (spec.get('model') or model).strip() or model
            host = (spec.get('executionHost') or host).strip() or host
            prov = (spec.get('provider') or prov).strip().lower() or prov
        model = resolve_cloud_agent_model(prov, model, default_model)
        defs.append(
            {
                'role': role,
                'model': model,
                'execution_host': host,
                'llm_provider': prov,
            }
        )
    return defs


def build_workers():
    workers = []
    for role_def in load_agent_role_defs():
        for idx in range(1, max(WORKERS_PER_ROLE, 1) + 1):
            workers.append(
                {
                    'name': f"{role_def['role']}-worker-{idx}",
                    'role': role_def['role'],
                    'model': role_def['model'],
                    'execution_host': role_def['execution_host'],
                    'llm_provider': role_def['llm_provider'],
                }
            )
    return workers


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
    elif role == 'intake':
        must = [
            {'term': {'status': 'ready'}},
            {'bool': {'should': [
                {'term': {'assigned_agent_role': 'intake'}},
                {'term': {'owner': 'intake'}},
            ], 'minimum_should_match': 1}},
        ]
    elif role == 'memory-updater':
        must = [
            {'term': {'status': 'ready'}},
            {'bool': {'should': [
                {'term': {'assigned_agent_role': 'memory-updater'}},
                {'term': {'owner': 'memory-updater'}},
            ], 'minimum_should_match': 1}},
        ]
    else:
        log(f'unknown role in ready_items_for_role: {role}')
        return []
    body = {
        'size': 20,
        'query': {'bool': {'must': must}},
        'sort': [
            {'updated_at': {'order': 'asc', 'unmapped_type': 'date'}}
        ]
    }
    return es_request(f'/{TASK_INDEX}/_search', body, method='GET').get('hits', {}).get('hits', [])


def claim(
    item_id,
    role,
    execution_host=None,
    preferred_model=None,
    worker_name=None,
    preferred_llm_provider=None,
):
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
    if preferred_llm_provider:
        doc['preferred_llm_provider'] = preferred_llm_provider
    if worker_name:
        doc['active_worker'] = worker_name
    es_request(f'/{TASK_INDEX}/_update/{item_id}', {'doc': doc}, method='POST')


def save_state(state):
    BASE.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + '\n')


def cycle():
    # Re-merge .env + OpenBao, then same LLM_* resolution as the dashboard (load_effective_pairs).
    apply_runtime_config(_WS)
    sync_llm_env_from_workspace(_WS)
    workers = build_workers()
    state = {'updated_at': now_iso(), 'workers': []}
    for worker in workers:
        snapshot = dict(worker)
        snapshot['heartbeat_at'] = now_iso()
        snapshot['status'] = 'idle'
        snapshot['current_task_id'] = None
        hits = ready_items_for_role(worker['role'])
        if hits:
            task = hits[0]
            item_id = task.get('_id')
            src = task.get('_source', {})
            pref_model = src.get('preferred_model') or worker['model']
            pref_prov = src.get('preferred_llm_provider') or worker.get('llm_provider')
            claim(
                item_id,
                worker['role'],
                worker.get('execution_host'),
                pref_model,
                worker['name'],
                pref_prov,
            )
            snapshot['status'] = 'claimed'
            snapshot['current_task_id'] = src.get('id', item_id)
            snapshot['current_task_title'] = src.get('title')
            snapshot['preferred_model'] = pref_model
            snapshot['preferred_llm_provider'] = pref_prov
            log(f"{worker['name']} claimed {snapshot['current_task_id']}")
        state['workers'].append(snapshot)
    save_state(state)


def main():
    if not ES_API_KEY or ES_API_KEY == 'AUTO_GENERATED_BY_INSTALLER':
        raise SystemExit(
            'ES_API_KEY is required. Store it in OpenBao (KV secret/flume) or .env — see install/flume.config.example.json'
        )
    log('worker manager starting')
    while True:
        try:
            cycle()
        except Exception as e:
            log(f'cycle error: {e}')
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
