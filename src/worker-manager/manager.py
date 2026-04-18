#!/usr/bin/env python3
import json
import os
import ssl
import sys
import time
# AST injected by Swarm Agent 3
import socket
import urllib.request
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from http.server import BaseHTTPRequestHandler, HTTPServer

NODE_ID = os.environ.get('HOSTNAME') or socket.gethostname() or "null-node"

_WS = Path(__file__).resolve().parent.parent
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))
from flume_secrets import apply_runtime_config  # noqa: E402
from workspace_llm_env import resolve_cloud_agent_model, sync_llm_env_from_workspace  # noqa: E402
import llm_credentials_store as lcs  # noqa: E402

apply_runtime_config(_WS)

BASE = _WS / 'worker-manager'
from utils.workspace import resolve_safe_workspace  # noqa: E402
try:
    from dashboard.llm_settings import load_effective_pairs
    for _k, _v in load_effective_pairs(resolve_safe_workspace()).items():
        if _v is not None and str(_v).strip():
            os.environ[_k] = str(_v).strip()
except ImportError:
    pass

# AP-8: AGENT_MODELS_FILE (local disk) replaced by ES flume-config document.
# Kept as an optional file-based fallback during rollout so existing
# agent_models.json files are still honoured until explicitly migrated.
AGENT_MODELS_FILE = resolve_safe_workspace() / 'worker-manager' / 'agent_models.json'
AGENT_MODELS_ES_ID = 'agent-models'  # document ID in flume-config index

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


# AP-6: file_path logger arg removed — get_logger writes to stdout only.
from utils.logger import get_logger  # noqa: E402
_manager_logger = get_logger('worker-manager')


def _fetch_adaptive_max_concurrency(active_mesh_count: int) -> int:
    """Dynamically determine MAX_CONCURRENT_TASKS based on cluster VRAM constraints."""
    try:
        ceiling_gb = 12  # Default fallback
        res = es_request(f'/flume-node-registry/_search', {'size': 100}, method='GET')
        
        max_parallel = 4
        global_gridlock = False
        
        if res:
            nodes = res.get('hits', {}).get('hits', [])
            sum_gb = 0
            congestion_count = 0
            
            for n in nodes:
                src = n.get('_source', {})
                c = src.get('capabilities', {})
                health = src.get('health', {})
                
                mem = c.get('memory_gb', 0)
                if mem:
                    sum_gb += float(mem)
                    
                node_cap = src.get('concurrency_cap', 4)
                if node_cap > max_parallel:
                    max_parallel = node_cap
                    
                latency = health.get('latency_ms', 0)
                if latency > 20000:
                    congestion_count += 1
            
            if sum_gb > 0:
                ceiling_gb = sum_gb
            
            # Emergency Brake Evaluation
            if len(nodes) > 0 and congestion_count == len(nodes):
                global_gridlock = True
                
        ceiling_bytes = ceiling_gb * (1024**3)
        
        current_vram_bytes = 0
        try:
            import urllib.request
            import json
            req = urllib.request.Request('http://host.docker.internal:11434/api/ps', method='GET')
            with urllib.request.urlopen(req, timeout=1.0) as ps_resp:
                ps_data = json.loads(ps_resp.read().decode())
                for m in ps_data.get('models', []):
                    current_vram_bytes += float(m.get('size_vram', 0))
        except Exception:
            pass
            
        # Context Floor Constraint (approx 1.5GB KV cache allowance per active local agent)
        projected_total = current_vram_bytes + (active_mesh_count * 1.5 * (1024**3))
        
        # Adaptive Threshold Formula
        if global_gridlock:
            _manager_logger.critical(f"EMERGENCY BRAKE: Severe gridlock detected across all {congestion_count} nodes (Latency > 20s). Collapsing concurrency cap to 1.")
            return 1
            
        if projected_total > ceiling_bytes:
            return 1 # Choked VRAM, clamp to strictly 1 task execution
        elif projected_total > ceiling_bytes * 0.75:
            return max(1, int(max_parallel * 0.5)) # partial throttle natively
        else:
            return max_parallel # Sub-ceiling, run at declarative capacity!
            
    except Exception as e:
        _manager_logger.error(f"Failed calculating adaptive concurrency: {e}")
        return 2

def log(msg, **kwargs):
    if kwargs:
        _manager_logger.info(str(msg), extra={'structured_data': kwargs})
    else:
        _manager_logger.info(str(msg))

def log_telemetry_event(worker_name: str, event_type: str, details: str, level: str = "INFO"):
    ts = now_iso()
    doc = {
        "@timestamp": ts,
        "timestamp": ts,
        "worker_name": worker_name,
        "event_type": event_type,
        "message": details,
        "level": level
    }
    try:
        es_request("/flume-telemetry/_doc", body=doc, method="POST")
    except Exception as e:
        log(f"telemetry logging failed: {e}")

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","service":"flume-worker"}')
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, format, *args):
        pass

def start_health_server():
    server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def load_agent_role_defs():
    """Merge per-role overrides from the ES flume-config document (AP-8) or
    the legacy agent_models.json file with LLM_* / EXECUTION_HOST env."""
    default_model = (os.environ.get('LLM_MODEL') or 'llama3.2').strip() or 'llama3.2'
    default_host = (os.environ.get('EXECUTION_HOST') or 'localhost').strip() or 'localhost'
    default_prov = os.environ.get('LLM_PROVIDER', 'ollama').strip().lower()
    cfg = {}
    # 1. Try ES flume-config first (K8s-native, replica-safe)
    try:
        es_url = ES_URL
        api_key = os.environ.get('ES_API_KEY', '')
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['Authorization'] = f'ApiKey {api_key}'
        req = urllib.request.Request(
            f'{es_url}/flume-config/_doc/{AGENT_MODELS_ES_ID}',
            headers=headers, method='GET',
        )
        with urllib.request.urlopen(req, context=ctx, timeout=3) as resp:
            raw = resp.read().decode()
            doc = json.loads(raw) if raw else {}
            src = doc.get('_source') or {}
            if src.get('roles'):
                cfg = src['roles']
    except Exception:
        pass  # fall through to file-based fallback
    # 2. File-based fallback (dev / migration period)
    if not cfg and AGENT_MODELS_FILE.is_file():
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
    """Return the default number of workers per agent role.

    P1: Capped to 2 to match realistic Ollama concurrency. The previous
    CPU_COUNT * 0.8 formula produced 6 workers/role on an 8-core Mac,
    spawning ~72 virtual workers across 2 replicas for only 2 Ollama slots.
    Override with WORKERS_PER_ROLE env var for higher concurrency with
    frontier providers that support many parallel requests.
    """
    return 2


def build_workers():
    workers = []
    limit = get_dynamic_worker_limit()
    raw = os.environ.get('WORKERS_PER_ROLE')
    if raw:
        try:
            limit = int(raw)
        except ValueError:
            limit = get_dynamic_worker_limit()

    for role_def in load_agent_role_defs():
        active_limit = 1 if role_def['role'] == 'pm' else limit
        for idx in range(1, active_limit + 1):
            workers.append(
                {
                    'name': f"{role_def['role']}-{NODE_ID}-worker-{idx}",
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
    """Query-only: return candidate tasks for a role without claiming them."""
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
        'seq_no_primary_term': True,
        'sort': [
            {'updated_at': {'order': 'asc', 'unmapped_type': 'date'}}
        ]
    }
    return es_request(f'/{TASK_INDEX}/_search', body, method='GET').get('hits', {}).get('hits', [])


def _normalize_title(title: str) -> str:
    """Lowercase, strip whitespace and punctuation for dedup comparison."""
    import re
    return re.sub(r'[^a-z0-9 ]', '', (title or '').lower()).strip()


def _is_duplicate_task(task_title: str, task_id: str) -> bool:
    """Check if a task with the same normalized title is already running, in review, or done.

    Returns True if a duplicate exists, meaning this task should be skipped
    to prevent parallel agents from redundantly executing the same work.
    """
    norm = _normalize_title(task_title)
    if not norm:
        return False
    try:
        body = {
            'size': 0,
            'query': {'bool': {'must': [
                {'bool': {'should': [
                    {'term': {'status': 'running'}},
                    {'term': {'status': 'review'}},
                    {'term': {'status': 'done'}},
                ], 'minimum_should_match': 1}},
            ], 'must_not': [
                {'term': {'_id': task_id}},
            ]}},
        }
        res = es_request(f'/{TASK_INDEX}/_search', body, method='GET')
        hits = res.get('hits', {}).get('hits', []) if res.get('hits', {}).get('total', {}).get('value', 0) > 0 else []
        # Full scan: fetch titles of active tasks and compare normalized
        if res.get('hits', {}).get('total', {}).get('value', 0) > 0:
            body['size'] = 50
            body['_source'] = ['title']
            res2 = es_request(f'/{TASK_INDEX}/_search', body, method='GET')
            for h in res2.get('hits', {}).get('hits', []):
                existing_title = _normalize_title(h.get('_source', {}).get('title', ''))
                if existing_title == norm:
                    log(f"dedup: skipping task '{task_title}' — duplicate of {h['_id']} already in progress")
                    return True
        return False
    except Exception as e:
        log(f"dedup check error: {e}")
        return False  # fail open — better to allow potential dups than block work


def _dedup_skip_task(task_id: str, reason: str):
    """Mark a task as skipped due to deduplication."""
    try:
        es_request(
            f'/{TASK_INDEX}/_update/{task_id}?refresh=true',
            {'doc': {
                'status': 'done',
                'queue_state': 'skipped',
                'active_worker': None,
                'updated_at': now_iso(),
                'agent_log': [{'note': f'Skipped: {reason}', 'ts': now_iso()}],
            }},
            method='POST',
        )
    except Exception as e:
        log(f"failed to mark task {task_id} as skipped: {e}")


def try_atomic_claim(
    role: str,
    worker_name: str,
    execution_host: Optional[str] = None,
    preferred_model: Optional[str] = None,
    preferred_llm_provider: Optional[str] = None,
    preferred_llm_credential_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Kubernetes-grade atomic task claim using a single _update_by_query roundtrip.

    Instead of fetch→CAS (which causes O(N²) 409 collisions under a swarm), this
    executes a Painless script that atomically transitions exactly one ``ready``
    task to ``running`` in a single ES operation — equivalent to:

        UPDATE tasks SET status='running', active_worker=? WHERE status='ready'
        AND role=? ORDER BY updated_at ASC LIMIT 1

    A per-worker random seed scatters which task each worker targets so the entire
    pool claims N *different* tasks concurrently instead of thundering on position 0.

    Returns the claimed task _source dict on success, or None if no task was available
    or the script raced with another worker (both of which are safe no-ops).
    """
    # Tester & reviewer pick up tasks in 'review' status (set by implementer handoff);
    # PM picks up 'planned'; all other roles pick up 'ready'.
    if role == 'pm':
        target_status = 'planned'
    elif role in ('tester', 'reviewer'):
        target_status = 'review'
    else:
        target_status = 'ready'

    # Build the role filter
    if role == 'pm':
        role_filter = {'bool': {
            'should': [
                {'term': {'owner': 'pm'}},
                {'term': {'assigned_agent_role': 'pm'}},
            ],
            'minimum_should_match': 1,
        }}
    else:
        role_filter = {'bool': {'should': [
            {'term': {'assigned_agent_role': role}},
            {'term': {'owner': role}},
        ], 'minimum_should_match': 1}}

    # Per-worker-seeded random score scatters task selection across the swarm.
    # Each worker hashes its name to a stable seed so the same worker consistently
    # picks up the same "slice" of tasks, minimising cross-worker contention.
    seed = abs(hash(worker_name)) % 2147483647

    query = {
        'function_score': {
            'query': {'bool': {'must': [
                {'term': {'status': target_status}},
                role_filter,
            ]}},
            'functions': [{'random_score': {'seed': seed, 'field': '_seq_no'}}],
            'boost_mode': 'replace',
        }
    }

    now = now_iso()
    new_status = 'running' if role != 'pm' else 'planned'
    script = {
        'source': (
            'if (ctx._source.status == params.expected_status '
            '&& (ctx._source.active_worker == null || ctx._source.active_worker == "")) {'
            '  ctx._source.status = params.new_status;'
            '  ctx._source.queue_state = "active";'
            '  ctx._source.active_worker = params.worker_name;'
            '  ctx._source.assigned_agent_role = params.role;'
            '  ctx._source.owner = params.role;'
            '  ctx._source.updated_at = params.now;'
            '  ctx._source.last_update = params.now;'
            '  if (params.execution_host != null) { ctx._source.execution_host = params.execution_host; }'
            '  if (params.preferred_model != null) { ctx._source.preferred_model = params.preferred_model; }'
            '  if (params.preferred_llm_provider != null) { ctx._source.preferred_llm_provider = params.preferred_llm_provider; }'
            '  if (params.preferred_llm_credential_id != null) { ctx._source.preferred_llm_credential_id = params.preferred_llm_credential_id; }'
            '} else {'
            '  ctx.op = "noop";'
            '}'
        ),
        'lang': 'painless',
        'params': {
            'expected_status': target_status,
            'new_status': new_status,
            'worker_name': worker_name,
            'role': role,
            'now': now,
            'execution_host': execution_host,
            'preferred_model': preferred_model,
            'preferred_llm_provider': preferred_llm_provider,
            'preferred_llm_credential_id': preferred_llm_credential_id,
        },
    }

    body = {
        'query': query,
        'script': script,
        'max_docs': 1,
        'sort': [
            {'priority': {'order': 'desc', 'unmapped_type': 'keyword'}},
            {'_score': {'order': 'desc'}}
        ]
    }

    try:
        res = es_request(
            f'/{TASK_INDEX}/_update_by_query?conflicts=proceed&refresh=true',
            body,
            method='POST',
        )
        updated = res.get('updated', 0)
        if updated != 1:
            # 0 updated: either no ready tasks or lost a benign race — both are fine
            return None

        # Fetch the doc we just claimed to return full task data to the caller
        # (needed so sync_worker_processes gets the task title/id for state tracking)
        hits = es_request(
            f'/{TASK_INDEX}/_search',
            {
                'size': 1,
                'query': {'term': {'active_worker': worker_name}},
                'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
                'seq_no_primary_term': True,
            },
            method='GET',
        ).get('hits', {}).get('hits', [])
        if hits:
            claimed = hits[0]
            claimed_src = claimed.get('_source', {})
            claimed_title = claimed_src.get('title', '')
            claimed_doc_id = claimed.get('_id', '')
            # Dedup gate: if an identical task is already active, release this claim
            if claimed_title and _is_duplicate_task(claimed_title, claimed_doc_id):
                _dedup_skip_task(claimed_doc_id, f'Duplicate of existing active task with title: {claimed_title}')
                return None
            return claimed
        return None
    except Exception as e:
        # Log but don't surface — callers treat None as "nothing available"
        log(f"atomic claim error for {worker_name}: {e}")
        return None


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
    if execution_host:
        doc['execution_host'] = execution_host
    if preferred_model:
        doc['preferred_model'] = preferred_model
    if preferred_llm_provider:
        doc['preferred_llm_provider'] = preferred_llm_provider
    if preferred_llm_credential_id:
        doc['preferred_llm_credential_id'] = preferred_llm_credential_id
    if worker_name:
        doc['active_worker'] = worker_name
    
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
    try:
        state['updated_at'] = now_iso()
        es_request(f'/agent-system-workers/_doc/{NODE_ID}', state, method='POST')
    except Exception as e:
        log(f"Error publishing worker state to ES: {e}")


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


def requeue_stuck_review_tasks() -> int:
    """
    Tester/reviewer tasks stuck in status=review with a stale updated_at are
    reset with active_worker cleared so a reviewer can reclaim them.

    This catches tasks where:
    - The tester passed to reviewer but the worker crashed before completing.
    - A reviewer worker died mid-evaluation.
    - The now-fixed active_worker bug left a phantom lock (defense in depth).

    Disabled when FLUME_STUCK_REVIEW_SECONDS is 0. Default 300 (5 minutes).
    Review evaluations are typically faster than implementations.
    """
    sec = int(os.environ.get('FLUME_STUCK_REVIEW_SECONDS', '300'))
    if sec <= 0:
        return 0
    body = {
        'size': 30,
        'query': {
            'bool': {
                'must': [
                    {'term': {'status': 'review'}},
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
        # Only requeue if someone is holding the lock (phantom active_worker)
        active = (src.get('active_worker') or '').strip()
        if not active:
            continue
        es_doc_id = h.get('_id')
        if not es_doc_id:
            continue
        try:
            es_request(
                f'/{TASK_INDEX}/_update/{es_doc_id}',
                {
                    'doc': {
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
                f"requeued stuck review task {tid} (stale for {stale:.0f}s, "
                f"active_worker was '{active}'; "
                f"threshold={sec}s, set FLUME_STUCK_REVIEW_SECONDS=0 to disable)"
            )
            n += 1
        except Exception as e:
            log(f"failed to requeue stuck review task {src.get('id')}: {e}")
    return n


def promote_planned_tasks() -> int:
    """
    Find tasks in status=planned. If all their depends_on tasks are status=done,
    transition them to status=ready.
    """
    body = {
        'size': 100,
        'query': {
            'term': {'status': 'planned'}
        }
    }
    try:
        res = es_request(f'/{TASK_INDEX}/_search', body, method='GET')
    except Exception:
        return 0
    n = 0
    for h in res.get('hits', {}).get('hits', []):
        src = h.get('_source', {})
        deps = src.get('depends_on', [])
        es_doc_id = h.get('_id')
        if not deps:
            # If no dependencies, promote it immediately
            try:
                es_request(
                    f'/{TASK_INDEX}/_update/{es_doc_id}',
                    {'doc': {'status': 'ready', 'updated_at': now_iso(), 'last_update': now_iso(), 'queue_state': 'queued'}},
                    method='POST',
                )
                log(f"promoted planned task {src.get('id', es_doc_id)} to ready (no dependencies)")
                n += 1
            except Exception as e:
                log(f"failed to promote planned task {es_doc_id}: {e}")
            continue
        
        # Check if all deps are done
        try:
            dep_res = es_request(f'/{TASK_INDEX}/_mget', {'ids': deps}, method='POST')
            docs = dep_res.get('docs', [])
            all_done = False
            if docs and all(d.get('found', False) and d.get('_source', {}).get('status') == 'done' for d in docs):
                all_done = True
                
            if all_done:
                es_request(
                    f'/{TASK_INDEX}/_update/{es_doc_id}',
                    {'doc': {'status': 'ready', 'updated_at': now_iso(), 'last_update': now_iso(), 'queue_state': 'queued'}},
                    method='POST',
                )
                log(f"promoted planned task {src.get('id', es_doc_id)} to ready (dependencies resolved)")
                n += 1
        except Exception as e:
            log(f"dependency sweep error for task {es_doc_id}: {e}")
            continue
            
    return n


import time
last_resume_timestamp = 0

def _execute_block_sweep(active_mesh_count: int, max_concurrent: int):
    """Pushes stalled ready tasks to the Blocked column to provide explicit Kanban feedback when cluster is saturated"""
    if active_mesh_count < max_concurrent:
        return
    try:
        body = {
            'query': {'term': {'status': 'ready'}},
            'script': {
                'source': (
                    'ctx._source.status = "blocked";'
                    'ctx._source.queue_state = "idle";'
                    'ctx._source.message = "Task paused due to local capacity limits (node overload). Will automatically resume when resources are available.";'
                ),
                'lang': 'painless'
            }
        }
        res = es_request(f'/{TASK_INDEX}/_update_by_query?conflicts=proceed', body, method='POST')
        updated = res.get('updated', 0)
        if updated > 0:
            log(f"Pushed {updated} capacity-stalled tasks to block queue", metric_id="flume_tasks_blocked_total", counter=updated)
    except Exception as e:
        _manager_logger.error(f"Failed to execute block sweep: {e}")

def _execute_resume_sweep(active_mesh_count: int, max_concurrent: int):
    global last_resume_timestamp
    if active_mesh_count >= max_concurrent:
        return
        
    now = time.time()
    if now - last_resume_timestamp < 60:
        return
        
    try:
        body = {
            'query': {'term': {'status': 'blocked'}},
            'script': {
                'source': (
                    'ctx._source.status = "ready";'
                    'ctx._source.queue_state = "idle";'
                    'ctx._source.active_worker = null;'
                ),
                'lang': 'painless'
            }
        }
        res = es_request(f'/{TASK_INDEX}/_update_by_query?conflicts=proceed', body, method='POST')
        updated = res.get('updated', 0)
        if updated > 0:
            last_resume_timestamp = now
            _manager_logger.info(f"Auto-Resumed {updated} blocked tasks safely due to cleared mesh capacity.")
    except Exception as e:
        _manager_logger.error(f"Failed to execute resume sweep: {e}")

def cycle():
    """Main orchestration heartbeat tick"""
    # 1. Check Global Cluster Paused state
    is_paused = False
    try:
        clust = es_request('/agent-system-cluster/_doc/config', method='GET')
        if clust and clust.get('_source', {}).get('status') == 'paused':
            is_paused = True
    except Exception:
        pass

    sync_llm_env_from_workspace(_WS)
    try:
        rq = requeue_stuck_implementer_tasks()
        if rq:
            log(f"stuck-implementer sweep: requeued {rq} task(s)")
    except Exception as e:
        log(f"stuck-implementer sweep error: {e}")
    try:
        rq_rev = requeue_stuck_review_tasks()
        if rq_rev:
            log(f"stuck-review sweep: cleared {rq_rev} phantom lock(s)")
    except Exception as e:
        log(f"stuck-review sweep error: {e}")
        
    try:
        promoted = promote_planned_tasks()
        if promoted:
            log(f"dependency sweep: promoted {promoted} task(s) to ready")
    except Exception as e:
        log(f"dependency sweep error: {e}")
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
                busy_workers[wn] = {
                    'task_id': s.get('id', h.get('_id')), 
                    'task_title': s.get('title'),
                    'execution_host': s.get('execution_host')
                }
    except Exception as e:
        log(f"error fetching busy workers: {e}")

    workers = build_workers()
    
    cloud_providers = {'openai', 'anthropic', 'google', 'azure'}
    active_mesh_count = 0
    
    # Pre-calculate active non-cloud nodes
    for wn, wdat in busy_workers.items():
        matching_worker = next((w for w in workers if w['name'] == wn), None)
        if matching_worker:
            w_prov = (matching_worker.get('llm_provider') or 'ollama').lower()
            if w_prov not in cloud_providers:
                active_mesh_count += 1
                
    max_concurrent = _fetch_adaptive_max_concurrency(active_mesh_count)
    
    # Auto-resume sweep to recover Blocked tasks when capacity allows securely
    _execute_resume_sweep(active_mesh_count, max_concurrent)
    _execute_block_sweep(active_mesh_count, max_concurrent)
    
    state = {'updated_at': now_iso(), 'workers': []}
    for worker in workers:
        snapshot = dict(worker)
        snapshot['heartbeat_at'] = now_iso()
        
        if worker['name'] in busy_workers:
            snapshot['status'] = 'claimed'
            snapshot['current_task_id'] = busy_workers[worker['name']]['task_id']
            snapshot['current_task_title'] = busy_workers[worker['name']]['task_title']
            
            # Adopt the dynamic execution host provided by Gateway / Node Mesh
            if busy_workers[worker['name']].get('execution_host'):
                snapshot['execution_host'] = busy_workers[worker['name']]['execution_host']
                
            wcid = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            snapshot['preferred_llm_credential_id'] = wcid
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, wcid)
            state['workers'].append(snapshot)
            continue
            
        snapshot['status'] = 'idle'
        snapshot['current_task_id'] = None
        if is_paused:
            # Cluster is globally disabled. Heartbeat state as idle but do not pull new tasks
            wcid = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            snapshot['preferred_llm_credential_id'] = wcid
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, wcid)
            state['workers'].append(snapshot)
            continue

        pref_model = worker['model']
        pref_prov = worker.get('llm_provider')
        pref_cred = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID

        check_prov = (pref_prov or 'ollama').lower()
        if check_prov not in cloud_providers and active_mesh_count >= max_concurrent:
            snapshot['status'] = 'idle'
            wcid = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            snapshot['preferred_llm_credential_id'] = wcid
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, wcid)
            state['workers'].append(snapshot)
            log(f"{worker['name']} throttled from claiming task to protect local Node Mesh. (MAX_CONCURRENT_TASKS={max_concurrent})", metric_id="flume_concurrency_throttled_total")
            continue

        claimed_task = try_atomic_claim(
            worker['role'],
            worker['name'],
            worker.get('execution_host'),
            pref_model,
            pref_prov,
            pref_cred,
        )
        if claimed_task:
            if check_prov not in cloud_providers:
                active_mesh_count += 1
            src = claimed_task.get('_source', {})
            snapshot['status'] = 'claimed'
            snapshot['current_task_id'] = src.get('id', claimed_task.get('_id'))
            snapshot['current_task_title'] = src.get('title')
            snapshot['preferred_model'] = pref_model
            snapshot['preferred_llm_provider'] = pref_prov
            snapshot['preferred_llm_credential_id'] = pref_cred
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, pref_cred)
            log(f"{worker['name']} claimed {snapshot['current_task_id']}")
            log_telemetry_event(worker['name'], "TASK_CLAIM", f"Claimed task: {(snapshot['current_task_title'] or '')[:50]}")
        else:
            snapshot['status'] = 'idle'
            wcid = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            snapshot['preferred_llm_credential_id'] = wcid
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, wcid)
        state['workers'].append(snapshot)
    
    save_state(state)
    if not is_paused:
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
                stdout=sys.stdout,
                stderr=sys.stderr,
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
        
    def ping_local_llm():
        raw = os.environ.get("LLM_BASE_URL") or os.environ.get("LOCAL_OLLAMA_BASE_URL", "http://host.docker.internal:11434")
        # Strip /v1 suffix — Ollama's native API endpoints are at /api/*, not /v1/api/*
        url = raw.rstrip('/').removesuffix('/v1')
        if "docker" in url and sys.platform.startswith("linux"):
            log("host.docker.internal natively detected on Linux!", event="linux_network_warning", url=url, advice="define LOCAL_LLM_HOST=172.17.0.1 in .env")
        try:
            req = urllib.request.Request(f"{url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3):
                pass
        except Exception as e:
            log("Local LLM boot ping failed", event="llm_ping_failure", url=url, error=str(e), advice="Workers may stall if unreachable")

    ping_local_llm()
    log('worker manager starting')
    while True:
        try:
            cycle()
        except Exception as e:
            log(f'cycle error: {e}')
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    from ast_poller import start_poller_thread  # type: ignore
    start_health_server()
    start_poller_thread()
    main()
