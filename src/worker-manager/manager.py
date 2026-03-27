#!/usr/bin/env python3
import json
import os
import ssl
import sys
import time
import asyncio
# AST injected by Swarm Agent 3
import urllib.request
import subprocess
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_WS = Path(__file__).resolve().parent.parent
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))
from flume_secrets import apply_runtime_config  # noqa: E402
from workspace_llm_env import resolve_cloud_agent_model, sync_llm_env_from_workspace  # noqa: E402
import llm_credentials_store as lcs  # noqa: E402

apply_runtime_config(_WS)

BASE = _WS / 'worker-manager'
from utils.workspace import resolve_safe_workspace
try:
    from dashboard.llm_settings import load_effective_pairs
    for _k, _v in load_effective_pairs(resolve_safe_workspace()).items():
        if _v is not None and str(_v).strip():
            os.environ[_k] = str(_v).strip()
except ImportError:
    pass

STATE = resolve_safe_workspace() / 'worker_state.json'
AGENT_MODELS_FILE = resolve_safe_workspace() / 'worker-manager' / 'agent_models.json'

ES_URL = os.environ.get('ES_URL', 'http://elasticsearch:9200').rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY', '')
ES_VERIFY_TLS = os.environ.get('ES_VERIFY_TLS', 'false').lower() == 'true'
TASK_INDEX = os.environ.get('ES_INDEX_TASKS', 'agent-task-records')
POLL_SECONDS = int(os.environ.get('WORKER_MANAGER_POLL_SECONDS', '2'))

active_worker_processes = {}

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


import logging
from logging.handlers import RotatingFileHandler

class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": now_iso(),
            "level": record.levelname,
            "message": record.getMessage(),
            "service": "worker-manager",
            "pid": os.getpid()
        })

_manager_logger = logging.getLogger('worker-manager')
_manager_logger.setLevel(logging.INFO)

try:
    _log_dir_env = os.environ.get('FLUME_LOG_DIR', '').strip()
    from utils.workspace import resolve_safe_workspace
    _log_dir = Path(_log_dir_env).resolve() if _log_dir_env else resolve_safe_workspace() / 'logs'
    _log_dir.mkdir(parents=True, exist_ok=True)
    _fh = RotatingFileHandler(_log_dir / 'manager.log', maxBytes=10*1024*1024, backupCount=5)
    _fh.setFormatter(JSONFormatter())
    _manager_logger.addHandler(_fh)
except PermissionError:
    pass

def log(msg):
    _manager_logger.info(str(msg))


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
        cred_id = ''
        if isinstance(spec, str):
            model = spec.strip() or model
        elif isinstance(spec, dict):
            model = (spec.get('model') or model).strip() or model
            host = (spec.get('executionHost') or host).strip() or host
            prov = (spec.get('provider') or prov).strip().lower() or prov
            cred_id = str(spec.get('credentialId') or spec.get('credential_id') or '').strip()
        if not cred_id:
            cred_id = lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
        model = resolve_cloud_agent_model(prov, model, default_model)
        defs.append(
            {
                'role': role,
                'model': model,
                'execution_host': host,
                'llm_provider': prov,
                'llm_credential_id': cred_id,
            }
        )
    return defs


def get_dynamic_worker_limit() -> int:
    try:
        cores = os.cpu_count() or 4
        # Reserve ~20% overhead for macOS Desktop & Elasticsearch buffers natively
        available = max(1, int(cores * 0.8))
        return available
    except BaseException as os_err:
        return 4


def build_workers():
    workers = []
    limit = get_dynamic_worker_limit()
    raw = os.environ.get('WORKERS_PER_ROLE')
    if raw:
        try:
            limit = int(raw)
        except ValueError as err:
            limit = get_dynamic_worker_limit()

    for role_def in load_agent_role_defs():
        active_limit = 1 if role_def['role'] == 'pm' else limit
        for idx in range(1, active_limit + 1):
            workers.append(
                {
                    'name': f"{role_def['role']}-worker-{idx}",
                    'role': role_def['role'],
                    'model': role_def['model'],
                    'execution_host': role_def['execution_host'],
                    'llm_provider': role_def['llm_provider'],
                    'llm_credential_id': role_def.get('llm_credential_id') or '',
                }
            )
    return workers


def es_request(path, body=None, method='GET'):
    es_key_val = os.environ.get("ES_API_KEY", "")
    es_url_val = os.environ.get("ES_URL", "http://elasticsearch:9200").rstrip("/")
    log(f"DEBUG: es_request hitting {es_url_val}{path} with payload: {json.dumps(body) if body else 'None'}")
    headers = {'Authorization': f'ApiKey {es_key_val}'}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
        # ES expects POST for JSON search bodies; GET+body is unreliable behind proxies.
        if method == 'GET':
            method = 'POST'
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
            {'term': {'status': 'ready'}},
            {'bool': {'should': [
                {'term': {'assigned_agent_role': 'tester'}},
                {'term': {'owner': 'tester'}},
            ], 'minimum_should_match': 1}},
        ]
    elif role == 'reviewer':
        must = [
            {'term': {'status': 'ready'}},
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
        'seq_no_primary_term': True,
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
    preferred_llm_credential_id=None,
    seq_no=None,
    primary_term=None,
):
    doc = {
        'status': 'running' if role != 'pm' else 'planned',
        'queue_state': 'active',
        'assigned_agent_role': role,
        'owner': role,
        'updated_at': now_iso(),
        'last_update': now_iso(),
    }
    if execution_host: doc['execution_host'] = execution_host
    if preferred_model: doc['preferred_model'] = preferred_model
    if preferred_llm_provider: doc['preferred_llm_provider'] = preferred_llm_provider
    if preferred_llm_credential_id: doc['preferred_llm_credential_id'] = preferred_llm_credential_id
    if worker_name: doc['active_worker'] = worker_name
    
    endpoint = f'/{TASK_INDEX}/_update/{item_id}?refresh=true'
    # Distributed Task Lease Coordinator: Prevent Thundering Herd via OCC Mutex Locks
    if seq_no is not None and primary_term is not None:
        endpoint += f'&if_seq_no={seq_no}&if_primary_term={primary_term}'
        
    try:
        es_request(endpoint, {'doc': doc}, method='POST')
        return True
    except Exception as e:
        log(f"Manager Mutex Lock Collision (409) prevented for task {item_id}: {e}")
        return False


def save_state(state):
    BASE.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + '\n')


def _task_stale_seconds(src: dict) -> Optional[float]:
    """Seconds since updated_at or last_update, or None if not parseable."""
    for k in ('updated_at', 'last_update'):
        t = src.get(k)
        if not t:
            continue
        s = str(t).replace('Z', '+00:00')
        try:
            parsed = datetime.fromisoformat(s)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - parsed).total_seconds()
        except Exception:
            continue
    return None


def requeue_stuck_implementer_tasks() -> int:
    """
    Implementer tasks left in status=running with a stale updated_at/last_update are
    reset to ready so handlers can retry (crashed worker, failed ES lookups, hung LLM).

    Disabled when FLUME_STUCK_IMPLEMENTER_SECONDS is 0. Default 600 (10 minutes).
    Progress notes now bump last_update; a healthy run refreshes this every LLM step.
    """
    sec = int(os.environ.get('FLUME_STUCK_IMPLEMENTER_SECONDS', '600'))
    if sec <= 0:
        return 0
    body = {
        'size': 30,
        'query': {
            'bool': {
                'must': [
                    {'term': {'status': 'running'}},
                    {
                        'bool': {
                            'should': [
                                {'term': {'assigned_agent_role': 'implementer'}},
                                {'term': {'owner': 'implementer'}},
                            ],
                            'minimum_should_match': 1,
                        },
                    },
                ],
            },
        },
    }
    try:
        res = es_request(f'/{TASK_INDEX}/_search', body, method='GET')
    except Exception:
        return 0
    n = 0
    for h in res.get('hits', {}).get('hits', []):
        src = h.get('_source', {})
        stale = _task_stale_seconds(src)
        if stale is None or stale < sec:
            continue
        es_doc_id = h.get('_id')
        if not es_doc_id:
            continue
        try:
            es_request(
                f'/{TASK_INDEX}/_update/{es_doc_id}',
                {
                    'doc': {
                        'status': 'ready',
                        'active_worker': None,
                        'queue_state': 'queued',
                        'updated_at': now_iso(),
                        'last_update': now_iso(),
                    }
                },
                method='POST',
            )
            tid = src.get('id', es_doc_id)
            log(
                f"requeued stuck implementer task {tid} (no timestamp refresh for {stale:.0f}s; "
                f"threshold={sec}s, set FLUME_STUCK_IMPLEMENTER_SECONDS=0 to disable)"
            )
            n += 1
        except Exception as e:
            log(f"failed to requeue stuck task {src.get('id')}: {e}")
    return n


def cycle():
    pass
    sync_llm_env_from_workspace(_WS)
    try:
        rq = requeue_stuck_implementer_tasks()
        if rq:
            log(f"stuck-implementer sweep: requeued {rq} task(s)")
    except Exception as e:
        log(f"stuck-implementer sweep error: {e}")
    busy_workers = {}
    try:
        res = es_request(
            f'/{TASK_INDEX}/_search',
            {'size': 500, 'query': {'match': {'queue_state': 'active'}}},
            method='POST'
        )
        for h in res.get('hits', {}).get('hits', []):
            s = h.get('_source', {})
            wn = s.get('active_worker')
            if wn:
                busy_workers[wn] = {'task_id': s.get('id', h.get('_id')), 'task_title': s.get('title')}
    except Exception as e:
        log(f"error fetching busy workers: {e}")

    workers = build_workers()
    state = {'updated_at': now_iso(), 'workers': []}
    for worker in workers:
        snapshot = dict(worker)
        snapshot['heartbeat_at'] = now_iso()
        
        if worker['name'] in busy_workers:
            snapshot['status'] = 'claimed'
            snapshot['current_task_id'] = busy_workers[worker['name']]['task_id']
            snapshot['current_task_title'] = busy_workers[worker['name']]['task_title']
            wcid = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            snapshot['preferred_llm_credential_id'] = wcid
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, wcid)
            state['workers'].append(snapshot)
            continue
            
        snapshot['status'] = 'idle'
        snapshot['current_task_id'] = None
        hits = ready_items_for_role(worker['role'])
        if hits:
            task = hits[0]
            item_id = task.get('_id')
            src = task.get('_source', {})
            # Role config in agent_models.json must win when a worker claims a task.
            # Older tasks may carry stale preferred_model / provider / credential values from
            # queue generation or a previous claim; re-stamping them here keeps runtime settings
            # authoritative and prevents task-local defaults from overriding the Agents UI.
            pref_model = worker['model']
            pref_prov = worker.get('llm_provider')
            pref_cred = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            
            # Request the Mutex Lock via Elasticsearch _seq_no tracking to prevent swarms
            if claim(
                item_id,
                worker['role'],
                worker.get('execution_host'),
                pref_model,
                worker['name'],
                pref_prov,
                pref_cred,
                seq_no=task.get('_seq_no'),
                primary_term=task.get('_primary_term')
            ):
                snapshot['status'] = 'claimed'
                snapshot['current_task_id'] = src.get('id', item_id)
                snapshot['current_task_title'] = src.get('title')
                snapshot['preferred_model'] = pref_model
                snapshot['preferred_llm_provider'] = pref_prov
                snapshot['preferred_llm_credential_id'] = pref_cred
                snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, pref_cred)
                log(f"{worker['name']} claimed {snapshot['current_task_id']}")
            else:
                # OCC Mutex Lock denied (task snagged by a faster node in the cluster)
                snapshot['status'] = 'idle'
                wcid = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
                snapshot['preferred_llm_credential_id'] = wcid
                snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, wcid)
            
        else:
            wcid = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            snapshot['preferred_llm_credential_id'] = wcid
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, wcid)
        state['workers'].append(snapshot)
    save_state(state)
    sync_worker_processes(state)


def sync_worker_processes(state):
    claimed = [w for w in state.get('workers', []) if w.get('status') == 'claimed']
    for w in claimed:
        name = w.get('name')
        if not name:
            continue
        proc = active_worker_processes.get(name)
        if proc is None or proc.poll() is not None:
            active_worker_processes[name] = subprocess.Popen(
                [sys.executable, str(BASE / 'worker_handlers.py'), name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            log(f"manager: spawned dynamic swarm subprocess for worker [{name}] natively")
def main():
    apply_runtime_config(_WS)
    from flume_secrets import hydrate_secrets_from_openbao
    hydrate_secrets_from_openbao()
    if 'https' in ES_URL and (not os.environ.get("ES_API_KEY") or os.environ.get("ES_API_KEY") == 'AUTO_GENERATED_BY_INSTALLER'):
        raise SystemExit(
            'ES_API_KEY is required for TLS clusters. Store it in OpenBao (KV secret/flume) or .env'
        )
    log('worker manager starting')
    while True:
        try:
            cycle()
        except Exception as e:
            log(f'cycle error: {e}')
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    try:
        from ast_poller import start_poller_thread
        start_poller_thread()
    except Exception as e:
        log(f"Failed to boot AST Deterministic Poller natively: {e}")
    main()
