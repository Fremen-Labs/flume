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


def _fetch_node_concurrency_caps() -> dict:
    """Dynamically determine PER-NODE MAX_CONCURRENT_TASKS based on cluster constraints."""
    node_caps = {}
    try:
        res = es_request(f'/flume-node-registry/_search', {'size': 100}, method='GET')
        nodes = res.get('hits', {}).get('hits', []) if res else []
        for n in nodes:
            src = n.get('_source', {})
            node_id = src.get('id', n.get('_id'))
            
            node_cap = src.get('concurrency_cap', 4)
            health = src.get('health', {})
            latency = health.get('latency_ms', 0)
            
            # Emergency Brake: Gradual ramp-down limit instead of hard clamping to 1
            if latency > 20000:
                _manager_logger.critical(f"EMERGENCY BRAKE: Severe latency on {node_id} ({latency}ms). Ramping limit down by 1.")
                node_cap = max(1, node_cap - 1)
                
            node_caps[node_id] = node_cap

    except Exception as e:
        _manager_logger.error(f"Failed calculating adaptive per-node concurrency: {e}")
        
    # Default fallback for unknown locals
    if 'localhost' not in node_caps:
        node_caps['localhost'] = 4
        
    return node_caps

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


def _delete_remote_branch_for_task(task_src: dict) -> None:
    """
    Best-effort: delete the remote branch associated with *task_src* when the
    task is being skipped/dedup'd so we don't litter the repo with orphan
    branches. Skips deletion for branches that may be shared (story-scoped
    `feature/story-*` and protected names like main/develop).
    """
    branch = str(task_src.get('branch') or '').strip()
    repo_id = str(task_src.get('repo') or '').strip()
    if not branch or not repo_id:
        return
    protected = {'main', 'master', 'develop', 'trunk'}
    if branch in protected:
        return
    # Shared story-scoped branches may be referenced by sibling tasks. Only
    # delete per-task branches (bugfix/<task-id>, feature/<task-id>).
    if branch.startswith('feature/story-') or branch.startswith('bugfix/story-'):
        return
    try:
        proj_res = es_request(f'/flume-projects/_doc/{repo_id}', method='GET')
        src = (proj_res or {}).get('_source') or {}
    except Exception:
        src = {}
    if not src or not (src.get('repoUrl') or src.get('repo_url')):
        return
    try:
        from utils.git_host_client import get_git_client, GitHostNotFoundError, GitHostError  # noqa: PLC0415
        client = get_git_client(src)
        client.delete_remote_branch(branch)
        log(f"dedup_cleanup: deleted orphan remote branch {branch!r} for skipped task {task_src.get('id')}")
    except Exception as e:
        # Imported lazily; handle NotFound specifically if the exception module is available.
        try:
            from utils.git_host_client import GitHostNotFoundError  # noqa: PLC0415
            if isinstance(e, GitHostNotFoundError):
                return
        except Exception:
            pass
        log(f"dedup_cleanup: failed to delete {branch!r} for task {task_src.get('id')}: {e}")


def _dedup_skip_task(task_id: str, reason: str):
    """Mark a task as skipped due to deduplication and GC its orphan branch."""
    # Load the doc first so we can clean up its remote branch after marking done.
    task_src: dict = {}
    try:
        res = es_request(
            f'/{TASK_INDEX}/_doc/{task_id}',
            method='GET',
        )
        task_src = (res or {}).get('_source') or {}
    except Exception:
        task_src = {}
    try:
        es_request(
            f'/{TASK_INDEX}/_update/{task_id}?refresh=true',
            {'doc': {
                'status': 'done',
                'queue_state': 'skipped',
                'active_worker': None,
                'remote_branch_deleted': True,
                'updated_at': now_iso(),
                'agent_log': [{'note': f'Skipped: {reason}', 'ts': now_iso()}],
            }},
            method='POST',
        )
    except Exception as e:
        log(f"failed to mark task {task_id} as skipped: {e}")
    # Clean up the orphan branch on best-effort basis. Never let this raise into the claim path.
    try:
        if task_src:
            _delete_remote_branch_for_task(task_src)
    except Exception as e:
        log(f"dedup_cleanup: unexpected error for {task_id}: {e}")


_WIP_CACHE: dict = {
    'ts': 0.0,
    'saturated_repos': set(),
    'saturated_stories': set(),
    'in_flight_branches': set(),  # branch names currently occupying a WIP slot
}
# Cache TTL is deliberately tight: the aggregation is cheap (single count-only
# search) and caching too long lets concurrent workers race past the WIP cap
# before the shared count reflects the new `running` task.
_WIP_CACHE_TTL_SECONDS = 0.25


def _load_repo_wip_limits() -> dict:
    """Return {repo_id: max_running_per_repo} from flume-projects (0 = unlimited)."""
    try:
        from utils.concurrency_config import max_running_for_repo  # noqa: PLC0415
    except Exception:
        return {}
    try:
        res = es_request('/flume-projects/_search', {'size': 500, 'query': {'match_all': {}}}, method='GET')
        hits = res.get('hits', {}).get('hits', []) if res else []
        out = {}
        for h in hits:
            src = h.get('_source') or {}
            pid = src.get('id') or h.get('_id')
            if not pid:
                continue
            out[pid] = max_running_for_repo(src)
        return out
    except Exception as e:
        log(f"_load_repo_wip_limits: {e}")
        return {}


def _compute_saturated_scopes(force: bool = False) -> tuple:
    """Return (saturated_repos, saturated_stories, in_flight_branches).

    Saturation is measured in *distinct branches in flight per repo*, not raw
    task count. A branch is "in flight" when at least one of its tasks is in
    ``running``/``review`` or is ``blocked`` with an unmerged branch.

    - ``saturated_repos``: repos where distinct-branches-in-flight >=
      ``maxRunningPerRepo``. When the repo is saturated, the claim layer
      only allows claims on tasks whose ``branch`` is already in flight
      (i.e. "continue existing work"), not tasks that would cut a brand-new
      branch.
    - ``saturated_stories``: parent story IDs that already saturate the
      feature-level story-parallelism cap.
    - ``in_flight_branches``: the global set of branch names currently
      occupying any WIP slot, used by the claim query to allow continuation.

    Cached briefly to amortize the aggregation across the worker swarm.
    """
    now = time.time()
    if not force and (now - _WIP_CACHE['ts']) < _WIP_CACHE_TTL_SECONDS:
        return (
            _WIP_CACHE['saturated_repos'],
            _WIP_CACHE['saturated_stories'],
            _WIP_CACHE['in_flight_branches'],
        )

    try:
        repo_limits = _load_repo_wip_limits()
    except Exception:
        repo_limits = {}

    try:
        body = {
            'size': 0,
            # WIP includes any task whose branch is live but unmerged:
            #   running / review    – agent is actively working the branch
            #   blocked+branch/sha  – stuck mid-pipeline, branch still floating
            'query': {'bool': {
                'should': [
                    {'terms': {'status': ['running', 'review']}},
                    {'bool': {
                        'must': [
                            {'term': {'status': 'blocked'}},
                            {'bool': {'should': [
                                {'exists': {'field': 'branch'}},
                                {'exists': {'field': 'commit_sha'}},
                            ], 'minimum_should_match': 1}},
                        ],
                        'must_not': [{'term': {'pr_merged': True}}],
                    }},
                ],
                'minimum_should_match': 1,
                'must_not': [
                    {'terms': {'item_type': ['epic', 'feature', 'story']}},
                    {'term': {'owner': 'pm'}},
                    {'term': {'assigned_agent_role': 'pm'}},
                ],
            }},
            'aggs': {
                # For each repo, enumerate the distinct branches that are
                # in flight. ``len(buckets)`` is exact at the sizes we run.
                'by_repo': {
                    'terms': {'field': 'repo', 'size': 500},
                    'aggs': {
                        'branches': {'terms': {'field': 'branch', 'size': 200}},
                    },
                },
                'by_parent': {'terms': {'field': 'parent_id.keyword', 'size': 1000, 'missing': ''}},
                'all_branches': {'terms': {'field': 'branch', 'size': 500}},
            },
        }
        res = es_request(f'/{TASK_INDEX}/_search', body, method='POST')
    except Exception as e:
        try:
            log(f"_compute_saturated_scopes: agg failed {e!r} body={json.dumps(body)[:500]}")
        except Exception:
            log(f"_compute_saturated_scopes: agg failed {e}")
        res = {}

    saturated_repos = set()
    distinct_branch_counts: dict = {}
    for b in (res.get('aggregations', {}).get('by_repo', {}).get('buckets', []) or []):
        key = b.get('key')
        if not key:
            continue
        branch_buckets = (b.get('branches') or {}).get('buckets') or []
        # Unique branches with a non-empty name. Tasks whose branch field is
        # missing also hit this agg as the "" bucket, which we filter out —
        # those tasks haven't cut a branch yet so they don't count against
        # saturation (but the claim gate will still block them from cutting
        # a new one when the repo is saturated).
        distinct = sum(1 for bb in branch_buckets if (bb.get('key') or '').strip())
        distinct_branch_counts[key] = distinct
        limit = repo_limits.get(key, 0)
        if limit and distinct >= limit:
            saturated_repos.add(key)
    # Repos with unknown limits fall back to env default via module helper
    if repo_limits.get('__default__') is None:
        try:
            from utils.concurrency_config import max_running_for_repo as _mrf  # noqa
            default_limit = _mrf(None)
        except Exception:
            default_limit = 0
        if default_limit:
            for key, cnt in distinct_branch_counts.items():
                if key and key not in repo_limits and cnt >= default_limit:
                    saturated_repos.add(key)

    # Global branch allow-list: any branch currently occupying a WIP slot
    # may receive additional claims (those don't create NEW branches).
    in_flight_branches = {
        (b.get('key') or '').strip()
        for b in (res.get('aggregations', {}).get('all_branches', {}).get('buckets', []) or [])
        if (b.get('key') or '').strip()
    }

    saturated_stories: set = set()
    try:
        from utils.concurrency_config import story_parallelism  # noqa
        default_story = story_parallelism(None)
    except Exception:
        default_story = 0
    if default_story:
        parent_counts = {
            b.get('key'): int(b.get('doc_count', 0) or 0)
            for b in (res.get('aggregations', {}).get('by_parent', {}).get('buckets', []) or [])
            if b.get('key')
        }
        for pid, cnt in parent_counts.items():
            if cnt >= default_story:
                saturated_stories.add(pid)

    _WIP_CACHE['ts'] = now
    _WIP_CACHE['saturated_repos'] = saturated_repos
    _WIP_CACHE['saturated_stories'] = saturated_stories
    _WIP_CACHE['in_flight_branches'] = in_flight_branches
    return saturated_repos, saturated_stories, in_flight_branches


PAINLESS_CLAIM_SCRIPT = (
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
)

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

    # WIP gate: enforce "one branch at a time" style serialization for
    # implementer claims. Tasks that would CONTINUE an already-in-flight
    # branch (same ``branch`` field) are still claimable — we only want to
    # block claims that would cut a NEW branch while a saturated repo /
    # story is still draining. Testers/reviewers always drain `review` so
    # in-flight work can complete; we only gate the front door.
    must_not: list = []
    if role == 'implementer':
        try:
            saturated_repos, saturated_stories, in_flight_branches = _compute_saturated_scopes()
            allowed_branches = list(in_flight_branches)
            if saturated_repos:
                # Block claims where the repo is saturated AND the task
                # would cut a new branch (its ``branch`` is absent or not
                # among the already-in-flight set).
                if allowed_branches:
                    must_not.append({
                        'bool': {
                            'must': [{'terms': {'repo': list(saturated_repos)}}],
                            'must_not': [{'terms': {'branch': allowed_branches}}],
                        }
                    })
                else:
                    # No existing in-flight branches — just block the repo
                    # entirely (shouldn't happen when saturated > 0, but be
                    # safe).
                    must_not.append({'terms': {'repo': list(saturated_repos)}})
            if saturated_stories:
                # Same logic at the story level: only block claims that
                # would open a new branch under a saturated story. With
                # FLUME_BRANCH_SCOPE=story, sibling tasks under the same
                # story share one branch — continuing that branch must not
                # be blocked, otherwise ready tasks for an in-flight story
                # can never drain.
                if allowed_branches:
                    must_not.append({
                        'bool': {
                            'must': [{'terms': {'parent_id.keyword': list(saturated_stories)}}],
                            'must_not': [{'terms': {'branch': allowed_branches}}],
                        }
                    })
                else:
                    must_not.append({'terms': {'parent_id.keyword': list(saturated_stories)}})
        except Exception as e:
            log(f"wip gate skipped: {e}")

    bool_body = {'must': [
        {'term': {'status': target_status}},
        role_filter,
    ]}
    if must_not:
        bool_body['must_not'] = must_not

    query = {
        'function_score': {
            'query': {'bool': bool_body},
            'functions': [{'random_score': {'seed': seed, 'field': '_seq_no'}}],
            'boost_mode': 'replace',
        }
    }

    now = now_iso()
    new_status = 'running' if role != 'pm' else 'planned'
    script = {
        'source': PAINLESS_CLAIM_SCRIPT,
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


def _count_active_per_repo() -> dict:
    """Return {repo_id: count} of leaf tasks with an in-flight branch.

    Includes ``blocked`` tasks that still have a branch/commit_sha: a task
    blocked on a merge conflict (awaiting pr_reconcile rebase) still owns an
    unmerged branch, and promoting another ready task on top of it is what
    produces the multi-branch conflict cascade.
    """
    try:
        res = es_request(
            f'/{TASK_INDEX}/_search',
            {
                'size': 0,
                'query': {'bool': {
                    'should': [
                        {'terms': {'status': ['ready', 'running', 'review']}},
                        {'bool': {
                            'must': [
                                {'term': {'status': 'blocked'}},
                                {'bool': {'should': [
                                    {'exists': {'field': 'branch'}},
                                    {'exists': {'field': 'commit_sha'}},
                                ], 'minimum_should_match': 1}},
                            ],
                            'must_not': [{'term': {'pr_merged': True}}],
                        }},
                    ],
                    'minimum_should_match': 1,
                    'must_not': [
                        {'terms': {'item_type': ['epic', 'feature', 'story']}},
                        {'term': {'owner': 'pm'}},
                        {'term': {'assigned_agent_role': 'pm'}},
                    ],
                }},
                'aggs': {'by_repo': {'terms': {'field': 'repo', 'size': 500}}},
            },
            method='POST',
        )
    except Exception:
        return {}
    out = {}
    for b in (res.get('aggregations', {}).get('by_repo', {}).get('buckets', []) or []):
        key = b.get('key')
        if key:
            out[key] = int(b.get('doc_count', 0) or 0)
    return out


def _count_active_per_story() -> dict:
    """Return {parent_id: count} of leaf tasks with an in-flight branch.

    Mirrors ``_count_active_per_repo`` — includes blocked-with-branch so a
    merge-conflict task still occupies its story's parallelism slot.
    """
    try:
        res = es_request(
            f'/{TASK_INDEX}/_search',
            {
                'size': 0,
                'query': {'bool': {
                    'should': [
                        {'terms': {'status': ['ready', 'running', 'review']}},
                        {'bool': {
                            'must': [
                                {'term': {'status': 'blocked'}},
                                {'bool': {'should': [
                                    {'exists': {'field': 'branch'}},
                                    {'exists': {'field': 'commit_sha'}},
                                ], 'minimum_should_match': 1}},
                            ],
                            'must_not': [{'term': {'pr_merged': True}}],
                        }},
                    ],
                    'minimum_should_match': 1,
                    'must_not': [
                        {'terms': {'item_type': ['epic', 'feature', 'story']}},
                        {'term': {'owner': 'pm'}},
                        {'term': {'assigned_agent_role': 'pm'}},
                    ],
                }},
                'aggs': {'by_parent': {'terms': {'field': 'parent_id.keyword', 'size': 1000, 'missing': ''}}},
            },
            method='POST',
        )
    except Exception:
        return {}
    out = {}
    for b in (res.get('aggregations', {}).get('by_parent', {}).get('buckets', []) or []):
        key = b.get('key')
        if key:
            out[key] = int(b.get('doc_count', 0) or 0)
    return out


def promote_planned_tasks() -> int:
    """
    Find tasks in status=planned. If all their depends_on tasks are status=done,
    transition them to status=ready -- respecting per-repo maxReadyPerRepo and
    per-story storyParallelism so we don't stampede a single repo with branches.
    """
    body = {
        'size': 200,
        'query': {
            'term': {'status': 'planned'}
        },
        'sort': [{'updated_at': {'order': 'asc', 'unmapped_type': 'date'}}],
    }
    try:
        res = es_request(f'/{TASK_INDEX}/_search', body, method='POST')
    except Exception:
        return 0

    try:
        from utils.concurrency_config import max_ready_for_repo, story_parallelism  # noqa: PLC0415
    except Exception:
        max_ready_for_repo = lambda _p: 0  # noqa: E731
        story_parallelism = lambda _p: 0  # noqa: E731

    repo_limit_cache: dict = {}
    story_limit = story_parallelism(None)
    # `_compute_saturated_scopes` gives us both saturation and the set of
    # branches currently in flight. We reuse that so promotion decisions use
    # the same definition of "WIP slot" as the claim layer (one branch at a
    # time rather than one task at a time).
    try:
        _sat_repos, _sat_stories, _in_flight = _compute_saturated_scopes()
    except Exception:
        _sat_repos, _sat_stories, _in_flight = set(), set(), set()
    # branches-in-flight per repo (derived from active_by_repo buckets)
    active_by_story = _count_active_per_story() if story_limit else {}

    def _repo_limit(repo_id: str) -> int:
        if repo_id in repo_limit_cache:
            return repo_limit_cache[repo_id]
        try:
            proj_res = es_request(f'/flume-projects/_doc/{repo_id}', method='GET')
            src = (proj_res or {}).get('_source') or {}
        except Exception:
            src = {}
        limit = max_ready_for_repo(src) if src else max_ready_for_repo(None)
        repo_limit_cache[repo_id] = limit
        return limit

    rollup_types = {'epic', 'feature', 'story'}

    # Local, mutable copy: promoting a task onto a new branch reserves a slot.
    in_flight_branches = set(_in_flight)
    saturated_repos = set(_sat_repos)

    def _promote(es_doc_id: str, src: dict, reason: str) -> bool:
        nonlocal saturated_repos
        repo_id = src.get('repo') or ''
        parent_id = src.get('parent_id') or ''
        item_type = (src.get('item_type') or src.get('work_item_type') or 'task').lower()
        is_leaf = item_type not in rollup_types
        if is_leaf and repo_id:
            limit = _repo_limit(repo_id)
            prospective_branch = (src.get('branch') or '').strip()
            would_open_new_branch = (
                not prospective_branch
                or prospective_branch not in in_flight_branches
            )
            # Only block promotion when a NEW branch would be cut on a
            # repo that has already hit its cap. Continuations (same branch
            # already in flight) are always allowed because they don't
            # widen the merge surface.
            if limit and would_open_new_branch and repo_id in saturated_repos:
                return False
            if story_limit and parent_id and active_by_story.get(parent_id, 0) >= story_limit:
                return False
        try:
            es_request(
                f'/{TASK_INDEX}/_update/{es_doc_id}',
                {'doc': {'status': 'ready', 'updated_at': now_iso(), 'last_update': now_iso(), 'queue_state': 'queued'}},
                method='POST',
            )
        except Exception as e:
            log(f"failed to promote planned task {es_doc_id}: {e}")
            return False
        log(f"promoted planned task {src.get('id', es_doc_id)} to ready ({reason})")
        if is_leaf and repo_id:
            prospective_branch = (src.get('branch') or '').strip()
            if prospective_branch:
                in_flight_branches.add(prospective_branch)
            # Recompute saturation for this repo.
            limit = _repo_limit(repo_id)
            if limit:
                distinct = sum(
                    1 for b in in_flight_branches if b
                )  # global branches (across repos — fine for single-project deployments)
                if distinct >= limit:
                    saturated_repos.add(repo_id)
            if parent_id:
                active_by_story[parent_id] = active_by_story.get(parent_id, 0) + 1
        return True

    n = 0
    for h in res.get('hits', {}).get('hits', []):
        src = h.get('_source', {})
        deps = src.get('depends_on', [])
        es_doc_id = h.get('_id')
        if not deps:
            if _promote(es_doc_id, src, 'no dependencies'):
                n += 1
            continue
        try:
            dep_res = es_request(f'/{TASK_INDEX}/_mget', {'ids': deps}, method='POST')
            docs = dep_res.get('docs', [])
            if docs and all(d.get('found', False) and d.get('_source', {}).get('status') == 'done' for d in docs):
                if _promote(es_doc_id, src, 'dependencies resolved'):
                    n += 1
        except Exception as e:
            log(f"dependency sweep error for task {es_doc_id}: {e}")
            continue

    return n


import time
import random

last_resume_timestamp = 0

PAINLESS_RESUME_SCRIPT = (
    'ctx._source.status = "ready";'
    'ctx._source.queue_state = "idle";'
    'ctx._source.active_worker = null;'
)

PAINLESS_BLOCK_SCRIPT = (
    'ctx._source.status = "blocked";'
    'ctx._source.queue_state = "idle";'
    'ctx._source.message = "Task paused due to mesh capacity limits (node overload). Will automatically resume with jitter when resources free up.";'
)

def _execute_block_sweep(node_loads: dict, node_caps: dict, cloud_providers: set):
    """Pushes stalled ready tasks to the Blocked column to provide explicit Kanban feedback when cluster is saturated"""
    total_load = sum(node_loads.values())
    total_cap = sum(node_caps.values()) if node_caps else 4
    
    if total_load < total_cap:
        return
        
    try:
        body = {
            'query': {'term': {'status': 'ready'}},
            'script': {
                'source': PAINLESS_BLOCK_SCRIPT,
                'lang': 'painless'
            }
        }
        res = es_request(f'/{TASK_INDEX}/_update_by_query?conflicts=proceed', body, method='POST')
        updated = res.get('updated', 0)
        if updated > 0:
            log(f"Pushed {updated} capacity-stalled tasks to block queue", metric_id="flume_tasks_blocked_total", counter=updated)
    except Exception as e:
        _manager_logger.error(f"Failed to execute block sweep: {e}")

def _execute_resume_sweep():
    global last_resume_timestamp
    now = time.time()
    
    # Introduce Jitter for resuming (60s base + random 1-15s)
    if now - last_resume_timestamp < (60 + random.uniform(1, 15)):
        return
        
    try:
        body = {
            'query': {'term': {'status': 'blocked'}},
            'script': {
                'source': PAINLESS_RESUME_SCRIPT,
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
    node_loads = {}
    
    # Pre-calculate active loads dynamically natively per-node
    for wn, wdat in busy_workers.items():
        matching_worker = next((w for w in workers if w['name'] == wn), None)
        if matching_worker:
            w_prov = (matching_worker.get('llm_provider') or 'ollama').lower()
            if w_prov not in cloud_providers:
                h = wdat.get('execution_host') or matching_worker.get('execution_host') or 'localhost'
                node_loads[h] = node_loads.get(h, 0) + 1
                
    node_caps = _fetch_node_concurrency_caps()
    
    # Auto-resume sweep to recover Blocked tasks natively ensuring global mesh unlocks
    _execute_resume_sweep()
    _execute_block_sweep(node_loads, node_caps, cloud_providers)
    
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
        active_host = worker.get('execution_host') or 'localhost'
        host_load = node_loads.get(active_host, 0)
        host_cap = node_caps.get(active_host, 4)
        
        if check_prov not in cloud_providers and host_load >= host_cap:
            snapshot['status'] = 'idle'
            wcid = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            snapshot['preferred_llm_credential_id'] = wcid
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, wcid)
            state['workers'].append(snapshot)
            log(f"{worker['name']} throttled to protect local Node {active_host} (Node Cap={host_cap})", metric_id="flume_concurrency_throttled_total")
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
