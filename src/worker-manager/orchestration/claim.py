"""Atomic task claim pipeline for the Flume worker-manager.

Phase 7 Priority 6: Extracted from manager.py (lines 295-845).
Contains the dedup-aware, WIP-gated, Painless-scripted atomic claim
logic that safely transitions exactly one ``ready`` task to ``running``
in a single ES roundtrip.

Functions:
    try_atomic_claim      — Public entry point (instrumented with Prometheus)
    _try_atomic_claim_inner — Core claim logic
    _normalize_title      — Dedup title normalization
    _is_duplicate_task    — Running/review dedup check
    _delete_remote_branch_for_task — Best-effort branch cleanup
    _dedup_skip_task      — Mark duplicate as skipped + GC branch
    _load_repo_wip_limits — Per-repo concurrency config from ES
    _compute_saturated_scopes — WIP-gate aggregation (cached)
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

from config import NODE_ID, TASK_INDEX, now_iso
from es.client import es_request
from es.telemetry import log_task_state_transition, log_telemetry_event
from utils.logger import get_logger

logger = get_logger('orchestration.claim')

# ── Prometheus Instrumentation (Phase 10) ────────────────────────────────────
try:
    from observability.metrics import CLAIM_LATENCY, TASKS_CLAIMED
    _METRICS_ENABLED = True
except ImportError:
    _METRICS_ENABLED = False


def log(msg, **kwargs):
    if kwargs:
        logger.info(str(msg), extra={'structured_data': kwargs})
    else:
        logger.info(str(msg))


# ── Dedup Helpers ────────────────────────────────────────────────────────────

def _normalize_title(title: str) -> str:
    """Lowercase, strip whitespace and punctuation for dedup comparison."""
    return re.sub(r'[^a-z0-9 ]', '', (title or '').lower()).strip()


def _is_duplicate_task(task_title: str, task_id: str) -> bool:
    """Check if a task with the same normalized title is already running, in review, or done.

    Returns True if a duplicate exists, meaning this task should be skipped
    to prevent parallel agents from redundantly executing the same work.

    Phase 1.2: Merged the previous double-query (size:0 count + size:50 fetch)
    into a single query that fetches titles directly.
    """
    norm = _normalize_title(task_title)
    if not norm:
        return False
    try:
        body = {
            'size': 50,
            '_source': ['title'],
            'query': {'bool': {
                'must': [{'terms': {'status': ['running', 'review', 'done']}}],
                'must_not': [{'term': {'_id': task_id}}],
            }},
        }
        res = es_request(f'/{TASK_INDEX}/_search', body, method='GET')
        for h in res.get('hits', {}).get('hits', []):
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
    ``feature/story-*`` and protected names like main/develop).
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
        from utils.git_host_client import get_git_client, GitHostNotFoundError  # noqa: PLC0415
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


# ── WIP Gate / Saturation ────────────────────────────────────────────────────

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


# ── Painless Claim Script ────────────────────────────────────────────────────
# The raw Painless claim script is now pre-compiled into Elasticsearch natively
# during cluster bootstrap by the Flume Orchestrator, mapped to 'flume-task-claim'.
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


# ── Public API ───────────────────────────────────────────────────────────────

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

    Phase 10: Instrumented with CLAIM_LATENCY histogram and TASKS_CLAIMED counter.

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
    _claim_start = time.monotonic()
    try:
        return _try_atomic_claim_inner(
            role, worker_name, execution_host,
            preferred_model, preferred_llm_provider, preferred_llm_credential_id,
        )
    finally:
        if _METRICS_ENABLED:
            CLAIM_LATENCY.labels(role=role).observe(time.monotonic() - _claim_start)


def _try_atomic_claim_inner(
    role: str,
    worker_name: str,
    execution_host: Optional[str] = None,
    preferred_model: Optional[str] = None,
    preferred_llm_provider: Optional[str] = None,
    preferred_llm_credential_id: Optional[str] = None,
) -> Optional[dict]:
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
    seed = abs(hash(worker_name)) % 2147483647

    # WIP gate: enforce "one branch at a time" style serialization for
    # implementer claims.
    must_not: list = []
    if role == 'implementer':
        try:
            saturated_repos, saturated_stories, in_flight_branches = _compute_saturated_scopes()
            allowed_branches = list(in_flight_branches)
            if saturated_repos:
                if allowed_branches:
                    must_not.append({
                        'bool': {
                            'must': [{'terms': {'repo': list(saturated_repos)}}],
                            'must_not': [{'terms': {'branch': allowed_branches}}],
                        }
                    })
                else:
                    must_not.append({'terms': {'repo': list(saturated_repos)}})
            if saturated_stories:
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
        'id': 'flume-task-claim',
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
        start_ms = int(time.time() * 1000)
        if os.environ.get('FLUME_DEBUG_CLAIMS'):
            log(f"DEBUG: Executing try_atomic_claim for {worker_name} ({role})")
        res = es_request(
            f'/{TASK_INDEX}/_update_by_query?conflicts=proceed&refresh=true',
            body,
            method='POST',
        )
        roundtrip_ms = int(time.time() * 1000) - start_ms
        if os.environ.get('FLUME_DEBUG_CLAIMS'):
            log(f"DEBUG: try_atomic_claim result for {worker_name}: updated={res.get('updated', 0)}")

        updated = res.get('updated', 0)
        if updated != 1:
            return None

        # Fetch the doc we just claimed to return full task data to the caller
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

            # Emit telemetry for claim latency
            queue_delay_ms = 0
            if 'created_at' in claimed_src:
                try:
                    created_at_dt = datetime.fromisoformat(claimed_src['created_at'].replace('Z', '+00:00'))
                    queue_delay_ms = int((datetime.now(timezone.utc) - created_at_dt).total_seconds() * 1000)
                except Exception:
                    pass

            log_telemetry_event(
                worker_name,
                "task_claimed",
                f"Claimed task {claimed_doc_id} in {roundtrip_ms}ms (queue delay: {queue_delay_ms}ms)",
                level="INFO"
            )

            # Emit state transition for observability
            claimed_project = claimed_src.get('repo', '')
            log_task_state_transition(
                task_id=claimed_doc_id,
                prev_status=target_status,
                new_status=new_status,
                role=role,
                worker_name=worker_name,
                project=claimed_project
            )

            # Dedup gate: if an identical task is already active, release this claim
            if claimed_title and _is_duplicate_task(claimed_title, claimed_doc_id):
                _dedup_skip_task(claimed_doc_id, f'Duplicate of existing active task with title: {claimed_title}')
                return None

            # Phase 10: Increment claim counter on successful claim
            if _METRICS_ENABLED:
                TASKS_CLAIMED.labels(role=role, node_id=NODE_ID).inc()

            return claimed
        return None
    except Exception as e:
        # Log but don't surface — callers treat None as "nothing available"
        log(f"atomic claim error for {worker_name}: {e}")
        return None
