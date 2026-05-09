"""Sweep functions for the Flume worker-manager orchestration loop.

Phase 7 Priority 7: Extracted from manager.py (lines 857-1324).
Contains all periodic maintenance sweeps that keep the task pipeline
healthy: stuck task requeue, planned→ready promotion, block/resume,
and pre-flight availability counts.

Functions:
    requeue_stuck_implementer_tasks — Reset stale running tasks
    requeue_stuck_review_tasks      — Clear phantom review locks
    promote_planned_tasks           — Dependency-aware promotion
    execute_block_sweep             — Push stalled tasks to blocked
    execute_resume_sweep            — Auto-resume blocked tasks
    count_available_by_status       — Pre-flight msearch counts (delegated to es/queries.py)
    _task_stale_seconds             — Staleness helper (delegated to es/queries.py)
    _count_active_per_repo          — Aggregation: tasks per repo (delegated to es/queries.py)
    _count_active_per_story         — Aggregation: tasks per story (delegated to es/queries.py)
"""
import os
import random
import time

from config import TASK_INDEX, now_iso
from es.client import es_request
from es.queries import (
    task_stale_seconds as _task_stale_seconds,
    count_active_per_repo as _count_active_per_repo,
    count_active_per_story as _count_active_per_story,
    count_available_by_status,
)
from utils.logger import get_logger

logger = get_logger('orchestration.sweeps')


def log(msg, **kwargs):
    if kwargs:
        logger.info(str(msg), extra={'structured_data': kwargs})
    else:
        logger.info(str(msg))


# ── Sweep Interval Tracking (Phase 2.1) ─────────────────────────────────────
# Requeue sweeps have 300-600s thresholds — running them every 2s wastes ~2 ES
# calls per cycle. promote_planned has a tighter interval since it directly
# controls pipeline throughput.
SWEEP_LAST_RUN: dict = {'stuck_impl': 0, 'stuck_review': 0, 'promote': 0}
SWEEP_INTERVALS: dict = {
    'stuck_impl': 30,    # requeue_stuck_implementer_tasks: threshold is 600s
    'stuck_review': 30,  # requeue_stuck_review_tasks: threshold is 300s
    'promote': 5,        # promote_planned_tasks: tighter for throughput
}


# ── Query Helpers ────────────────────────────────────────────────────────────
# Phase 7 Priority 9: _task_stale_seconds, _count_active_per_repo,
# _count_active_per_story, and count_available_by_status are now canonical in
# es/queries.py and imported above. The private names are preserved for
# backward compatibility within this module.


# ── Stuck Task Requeue ───────────────────────────────────────────────────────

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

    Disabled when FLUME_STUCK_REVIEW_SECONDS is 0. Default 300 (5 minutes).
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


# ── Planned→Ready Promotion ─────────────────────────────────────────────────

def promote_planned_tasks() -> int:
    """
    Find tasks in status=planned. If all their depends_on tasks are status=done,
    transition them to status=ready -- respecting per-repo maxReadyPerRepo and
    per-story storyParallelism so we don't stampede a single repo with branches.
    """
    from orchestration.claim import _compute_saturated_scopes  # avoid circular; lazy

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
    try:
        _sat_repos, _sat_stories, _in_flight = _compute_saturated_scopes()
    except Exception:
        _sat_repos, _sat_stories, _in_flight = set(), set(), set()
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
            limit = _repo_limit(repo_id)
            if limit:
                distinct = sum(
                    1 for b in in_flight_branches if b
                )
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


# ── Block / Resume Sweeps ────────────────────────────────────────────────────

last_resume_timestamp = 0

# PAINLESS_RESUME_SCRIPT & PAINLESS_BLOCK_SCRIPT natively execute via ES pre-compiled scripts
# mapping cleanly through 'id': 'flume-task-resume' and 'id': 'flume-task-block'.

def execute_block_sweep(node_loads: dict, node_caps: dict, cloud_providers: set):
    """Pushes stalled ready tasks to the Blocked column to provide explicit Kanban feedback when cluster is saturated"""
    total_load = sum(node_loads.values())
    total_cap = sum(node_caps.values()) if node_caps else 4

    if total_load < total_cap:
        return

    try:
        body = {
            'query': {'term': {'status': 'ready'}},
            'script': {
                'id': 'flume-task-block',
            }
        }
        res = es_request(f'/{TASK_INDEX}/_update_by_query?conflicts=proceed', body, method='POST')
        updated = res.get('updated', 0)
        if updated > 0:
            log(f"Pushed {updated} capacity-stalled tasks to block queue", metric_id="flume_tasks_blocked_total", counter=updated)
    except Exception as e:
        logger.error(f"Failed to execute block sweep: {e}")


def execute_resume_sweep():
    global last_resume_timestamp
    now = time.time()

    # Introduce Jitter for resuming (60s base + random 1-15s)
    if now - last_resume_timestamp < (60 + random.uniform(1, 15)):
        return

    try:
        body = {
            'query': {'term': {'status': 'blocked'}},
            'script': {
                'id': 'flume-task-resume',
            }
        }
        res = es_request(f'/{TASK_INDEX}/_update_by_query?conflicts=proceed', body, method='POST')
        updated = res.get('updated', 0)
        if updated > 0:
            last_resume_timestamp = now
            logger.info(f"Auto-Resumed {updated} blocked tasks safely due to cleared mesh capacity.")
    except Exception as e:
        logger.error(f"Failed to execute resume sweep: {e}")


# ── Pre-flight Availability ──────────────────────────────────────────────────
# Phase 7 Priority 9: count_available_by_status is now canonical in
# es/queries.py and imported at the top of this module. The re-export
# through orchestration/__init__.py continues to work identically.
