"""
Autonomy sweeps: background sweeps that keep work flowing without human intervention.

Runs on a single daemon thread. Each sweep has its own cadence and state so a
slow sweep never starves the others.

Sweeps shipped here:

  • parent_revival
        When a child task (typically a `bug-*`) transitions to `done`, find its
        parent (via `parent_id` / `origin_task_id`, or by parsing the child id
        prefix `bug-<parent>-<n>`). If the parent is `blocked` without
        `needs_human`, append an `[Auto-recovery]` note pointing at the resolved
        child and transition the parent to `ready`. Idempotent via a
        `parent_revived_at` marker stamped on the child doc.

  • stuck_worker_watchdog
        Find tasks in `queue_state=active` (i.e. claimed) whose `last_update`
        is older than `FLUME_STUCK_TASK_MINUTES` (default 25). Clear the claim,
        drop an `[Auto-recovery]` note describing the timeout, and re-queue as
        `status=ready`. Respects `needs_human` and caps per-task retries via
        `stuck_recovery_count`.

Configuration (env):

  FLUME_AUTONOMY_ENABLED             default "1"
  FLUME_AUTONOMY_INTERVAL_SEC        default 60   — loop tick; each sweep also
                                                    has its own cadence.
  FLUME_PARENT_REVIVAL_INTERVAL_SEC  default 90
  FLUME_PARENT_REVIVAL_LOOKBACK_MIN  default 180  — window for "recently-done"
                                                    child tasks to consider.
  FLUME_PARENT_REVIVAL_MAX           default 25   — children processed per tick
  FLUME_STUCK_TASK_MINUTES           default 25   — claim idle threshold
  FLUME_STUCK_TASK_INTERVAL_SEC      default 120
  FLUME_STUCK_TASK_MAX               default 25   — rows processed per tick
  FLUME_STUCK_TASK_RETRY_CAP         default 3    — per-task recoveries before
                                                    we flag needs_human=true

The module is deliberately self-contained: dashboard server.py passes the ES
helpers and logger on startup.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    'enabled': False,
    'thread_alive': False,
    'config': {},
    'sweeps': {
        'parent_revival': {
            'last_run_at': None,
            'last_duration_ms': None,
            'last_summary': None,
            'runs': 0,
            'revived_total': 0,
            'errors_total': 0,
            'last_error': None,
        },
        'stuck_worker_watchdog': {
            'last_run_at': None,
            'last_duration_ms': None,
            'last_summary': None,
            'runs': 0,
            'recovered_total': 0,
            'escalated_total': 0,
            'errors_total': 0,
            'last_error': None,
        },
        'plan_progress_scan': {
            'last_run_at': None,
            'last_duration_ms': None,
            'last_summary': None,
            'runs': 0,
            'nudged_total': 0,
            'errors_total': 0,
            'last_error': None,
        },
    },
}

# ---- tiny utils --------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, '').strip() or default)
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name, '') or '').strip().lower()
    if not raw:
        return default
    return raw not in ('0', 'false', 'no', 'off')


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _parse_iso(ts: str) -> float | None:
    if not ts:
        return None
    try:
        s = ts.replace('Z', '+00:00')
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


_BUG_ID_RE = re.compile(r'^bug-(?P<parent>.+?)-\d+$')


def _infer_parent_id(child_src: dict) -> str | None:
    pid = (child_src.get('parent_id') or child_src.get('origin_task_id') or '').strip()
    if pid:
        return pid
    cid = (child_src.get('id') or '').strip()
    m = _BUG_ID_RE.match(cid)
    if m:
        return m.group('parent')
    return None


# ---- sweep: parent_revival ---------------------------------------------------


def _parent_revival_sweep(deps: dict) -> dict:
    logger = deps['logger']
    es_search = deps['es_search']
    es_post = deps['es_post']
    append_note = deps['append_note']

    lookback_min = _env_int('FLUME_PARENT_REVIVAL_LOOKBACK_MIN', 180)
    max_rows = _env_int('FLUME_PARENT_REVIVAL_MAX', 25)

    summary = {
        'scanned': 0,
        'revived': 0,
        'already_processed': 0,
        'parent_not_blocked': 0,
        'parent_not_found': 0,
        'errors': 0,
        'revived_ids': [],
    }

    since_ts = (time.time() - lookback_min * 60)
    since_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat().replace('+00:00', 'Z')

    # Query: recently-done children (bugs or tasks) with NO parent_revived_at stamp yet.
    body = {
        'size': max_rows * 2,
        'sort': [{'updated_at': {'order': 'desc'}}],
        'query': {
            'bool': {
                'must': [
                    {'term': {'status': 'done'}},
                    {'range': {'updated_at': {'gte': since_iso}}},
                ],
                'should': [
                    {'term': {'item_type': 'bug'}},
                    {'prefix': {'id': 'bug-'}},
                    {'exists': {'field': 'parent_id'}},
                ],
                'minimum_should_match': 1,
                'must_not': [
                    {'exists': {'field': 'parent_revived_at'}},
                ],
            }
        },
    }
    try:
        res = es_search('agent-task-records', body)
    except Exception as e:
        summary['errors'] += 1
        logger.warning(json.dumps({'event': 'parent_revival.query_failed', 'error': str(e)[:200]}))
        return summary

    hits = res.get('hits', {}).get('hits', []) or []
    summary['scanned'] = len(hits)

    processed = 0
    for h in hits:
        if processed >= max_rows:
            break
        child_id = h.get('_id')
        child_src = h.get('_source') or {}
        parent_logical_id = _infer_parent_id(child_src)
        if not parent_logical_id:
            # Stamp so we don't rescan this row forever.
            try:
                es_post(
                    f'agent-task-records/_update/{child_id}',
                    {'doc': {'parent_revived_at': _now_iso(), 'parent_revival_reason': 'no_parent_id'}},
                )
            except Exception:
                pass
            summary['parent_not_found'] += 1
            processed += 1
            continue

        # Look up the parent doc.
        try:
            pres = es_search('agent-task-records', {
                'size': 1,
                'query': {'bool': {'should': [
                    {'ids': {'values': [parent_logical_id]}},
                    {'term': {'id': parent_logical_id}},
                    {'term': {'id.keyword': parent_logical_id}},
                ], 'minimum_should_match': 1}},
            })
            phits = pres.get('hits', {}).get('hits', [])
        except Exception as e:
            summary['errors'] += 1
            logger.warning(json.dumps({
                'event': 'parent_revival.parent_lookup_failed',
                'child': child_src.get('id'),
                'parent_id': parent_logical_id,
                'error': str(e)[:200],
            }))
            continue

        if not phits:
            try:
                es_post(
                    f'agent-task-records/_update/{child_id}',
                    {'doc': {'parent_revived_at': _now_iso(), 'parent_revival_reason': 'parent_missing'}},
                )
            except Exception:
                pass
            summary['parent_not_found'] += 1
            processed += 1
            continue

        parent_hit = phits[0]
        parent_es_id = parent_hit.get('_id')
        parent_src = parent_hit.get('_source') or {}
        pstatus = (parent_src.get('status') or '').lower()
        pneeds_human = bool(parent_src.get('needs_human'))

        if pstatus != 'blocked' or pneeds_human:
            # Stamp the child so we skip it on subsequent sweeps.
            try:
                es_post(
                    f'agent-task-records/_update/{child_id}',
                    {'doc': {
                        'parent_revived_at': _now_iso(),
                        'parent_revival_reason': (
                            'parent_not_blocked' if pstatus != 'blocked' else 'parent_needs_human'
                        ),
                    }},
                )
            except Exception:
                pass
            summary['parent_not_blocked'] += 1
            processed += 1
            continue

        # Revive the parent.
        try:
            append_note(
                parent_es_id,
                (
                    f'[Auto-recovery] Child {child_src.get("id")} '
                    f'({child_src.get("item_type") or "task"}) resolved '
                    f'"{(child_src.get("title") or "")[:120]}". Re-queuing parent; '
                    'verify the original failure is gone and re-run tests.'
                ),
            )
            owner = parent_src.get('owner') or parent_src.get('assigned_agent_role')
            now = _now_iso()
            doc = {
                'status': 'ready',
                'queue_state': 'queued',
                'active_worker': None,
                'needs_human': False,
                'updated_at': now,
                'last_update': now,
                'implementer_consecutive_llm_failures': 0,
                'last_child_revived_from': child_src.get('id'),
            }
            if owner:
                doc['owner'] = owner
                doc['assigned_agent_role'] = owner
            es_post(f'agent-task-records/_update/{parent_es_id}', {'doc': doc})
            es_post(
                f'agent-task-records/_update/{child_id}',
                {'doc': {'parent_revived_at': _now_iso(), 'parent_revival_reason': 'revived'}},
            )
            summary['revived'] += 1
            summary['revived_ids'].append(parent_src.get('id'))
            processed += 1
            logger.info(json.dumps({
                'event': 'parent_revival.revived',
                'parent': parent_src.get('id'),
                'child': child_src.get('id'),
            }))
        except Exception as e:
            summary['errors'] += 1
            logger.warning(json.dumps({
                'event': 'parent_revival.process_failed',
                'parent': parent_src.get('id'),
                'child': child_src.get('id'),
                'error': str(e)[:300],
            }))

    return summary


# ---- sweep: stuck_worker_watchdog -------------------------------------------


def _stuck_worker_watchdog(deps: dict) -> dict:
    logger = deps['logger']
    es_search = deps['es_search']
    es_post = deps['es_post']
    append_note = deps['append_note']

    idle_min = _env_int('FLUME_STUCK_TASK_MINUTES', 25)
    max_rows = _env_int('FLUME_STUCK_TASK_MAX', 25)
    retry_cap = _env_int('FLUME_STUCK_TASK_RETRY_CAP', 3)

    summary = {
        'scanned': 0,
        'recovered': 0,
        'escalated': 0,
        'errors': 0,
        'recovered_ids': [],
        'escalated_ids': [],
    }

    cutoff_ts = time.time() - idle_min * 60
    cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat().replace('+00:00', 'Z')

    body = {
        'size': max_rows * 3,
        'sort': [{'last_update': {'order': 'asc', 'missing': '_last'}}],
        'query': {
            'bool': {
                'must': [
                    {'exists': {'field': 'active_worker'}},
                ],
                'should': [
                    {'term': {'queue_state': 'active'}},
                    {'term': {'status': 'running'}},
                    {'term': {'status': 'review'}},
                ],
                'minimum_should_match': 1,
                'must_not': [
                    {'term': {'needs_human': True}},
                    {'term': {'status': 'done'}},
                    {'term': {'status': 'archived'}},
                    {'term': {'status': 'cancelled'}},
                    {'term': {'status': 'blocked'}},
                ],
            }
        },
    }
    try:
        res = es_search('agent-task-records', body)
    except Exception as e:
        summary['errors'] += 1
        logger.warning(json.dumps({'event': 'stuck_worker.query_failed', 'error': str(e)[:200]}))
        return summary

    hits = res.get('hits', {}).get('hits', []) or []
    summary['scanned'] = len(hits)

    processed = 0
    for h in hits:
        if processed >= max_rows:
            break
        es_id = h.get('_id')
        src = h.get('_source') or {}
        last_raw = src.get('last_update') or src.get('updated_at') or src.get('created_at')
        last_ts = _parse_iso(last_raw) if last_raw else None
        if last_ts is None:
            # Treat missing timestamp as stale only if active_worker is set.
            if not src.get('active_worker'):
                continue
        else:
            if last_ts >= cutoff_ts:
                continue  # recent update — not stuck

        recovery_count = int(src.get('stuck_recovery_count') or 0)
        aw = src.get('active_worker')
        task_id = src.get('id')
        now = _now_iso()

        if recovery_count >= retry_cap:
            try:
                append_note(
                    es_id,
                    (
                        f'[Auto-recovery] Task stuck (no update for > {idle_min} min on worker {aw}). '
                        f'Giving up after {recovery_count} recoveries — needs human review.'
                    ),
                )
                es_post(f'agent-task-records/_update/{es_id}', {'doc': {
                    'needs_human': True,
                    'active_worker': None,
                    'queue_state': 'queued',
                    'stuck_recovery_count': recovery_count,
                    'stuck_last_recovery_at': now,
                    'updated_at': now,
                    'last_update': now,
                }})
                summary['escalated'] += 1
                summary['escalated_ids'].append(task_id)
                processed += 1
                logger.info(json.dumps({
                    'event': 'stuck_worker.escalated',
                    'task_id': task_id,
                    'idle_for_min': int((time.time() - (last_ts or cutoff_ts)) / 60),
                    'active_worker': aw,
                }))
            except Exception as e:
                summary['errors'] += 1
                logger.warning(json.dumps({
                    'event': 'stuck_worker.escalate_failed',
                    'task_id': task_id,
                    'error': str(e)[:200],
                }))
            continue

        try:
            idle_for = int((time.time() - (last_ts or cutoff_ts)) / 60)
            append_note(
                es_id,
                (
                    f'[Auto-recovery] Watchdog: no update for {idle_for} min on worker {aw}. '
                    'Releasing claim and re-queueing; the next implementer should continue from here.'
                ),
            )
            owner = src.get('owner') or src.get('assigned_agent_role')
            doc = {
                'status': 'ready',
                'queue_state': 'queued',
                'active_worker': None,
                'needs_human': False,
                'stuck_recovery_count': recovery_count + 1,
                'stuck_last_recovery_at': now,
                'updated_at': now,
                'last_update': now,
                'implementer_consecutive_llm_failures': 0,
            }
            if owner:
                doc['owner'] = owner
                doc['assigned_agent_role'] = owner
            es_post(f'agent-task-records/_update/{es_id}', {'doc': doc})
            summary['recovered'] += 1
            summary['recovered_ids'].append(task_id)
            processed += 1
            logger.info(json.dumps({
                'event': 'stuck_worker.recovered',
                'task_id': task_id,
                'idle_for_min': idle_for,
                'active_worker': aw,
                'attempts': recovery_count + 1,
            }))
        except Exception as e:
            summary['errors'] += 1
            logger.warning(json.dumps({
                'event': 'stuck_worker.process_failed',
                'task_id': task_id,
                'error': str(e)[:300],
            }))

    return summary


# ---- sweep: plan_progress_scan ----------------------------------------------


_ACTIONABLE_STATUSES = {'ready', 'running', 'review'}
_ROLLUP_ITEM_TYPES = {'epic', 'feature', 'story'}
_PLAN_CHECK_PREFIX = 'plan-check-'


def _plan_progress_scan(deps: dict) -> dict:
    """
    Detect projects where the queue has drained while the plan is still
    incomplete, and emit a `plan-check-*` task so the PM dispatcher can decide
    whether to spawn follow-up stories, close out the plan, or escalate.

    A project is considered stalled when ALL of these hold:
      - there is ≥1 rollup item (epic/feature/story) that is NOT done/archived/cancelled
      - there are 0 tasks in ready/running/review
      - there is ≥1 task in done state (we have actually started — guards against
        brand-new projects that have not been planned yet)

    Idempotency: we skip projects that already have an open (non-done)
    `plan-check-<repo>-…` task in the index.
    """
    logger = deps['logger']
    es_search = deps['es_search']
    es_post = deps['es_post']
    es_upsert = deps['es_upsert']
    append_note = deps['append_note']
    list_projects = deps.get('list_projects')
    if list_projects is None:
        return {'scanned': 0, 'nudged': 0, 'skipped': 0, 'errors': 0, 'nudged_ids': []}

    cooldown_min = _env_int('FLUME_PLAN_SCAN_COOLDOWN_MIN', 60)
    cooldown_ts = time.time() - cooldown_min * 60
    cooldown_iso = datetime.fromtimestamp(cooldown_ts, tz=timezone.utc).isoformat().replace('+00:00', 'Z')

    summary = {
        'scanned': 0, 'nudged': 0, 'skipped': 0, 'errors': 0,
        'skip_reasons': {}, 'nudged_ids': [],
    }

    try:
        projects = list_projects() or []
    except Exception as e:
        summary['errors'] += 1
        logger.warning(json.dumps({'event': 'plan_progress.list_projects_failed', 'error': str(e)[:200]}))
        return summary

    for proj in projects:
        repo_id = proj.get('id') if isinstance(proj, dict) else None
        if not repo_id:
            continue
        summary['scanned'] += 1
        try:
            res = es_search('agent-task-records', {
                'size': 500,
                'query': {'bool': {
                    'must': [{'term': {'repo': repo_id}}],
                    'must_not': [{'term': {'status': 'archived'}}],
                }},
                '_source': ['id', 'item_type', 'status', 'needs_human', 'updated_at'],
            })
        except Exception as e:
            summary['errors'] += 1
            logger.warning(json.dumps({
                'event': 'plan_progress.query_failed',
                'repo': repo_id, 'error': str(e)[:200],
            }))
            continue

        hits = res.get('hits', {}).get('hits', []) or []
        docs = [h.get('_source') or {} for h in hits]
        if not docs:
            summary['skipped'] += 1
            summary['skip_reasons']['empty_project'] = summary['skip_reasons'].get('empty_project', 0) + 1
            continue

        counts = {'actionable': 0, 'done': 0, 'open_rollup': 0, 'blocked_auto': 0, 'blocked_human': 0}
        for d in docs:
            st = (d.get('status') or '').lower()
            it = (d.get('item_type') or '').lower()
            if st in _ACTIONABLE_STATUSES:
                counts['actionable'] += 1
            if st == 'done':
                counts['done'] += 1
            if st == 'blocked':
                if d.get('needs_human'):
                    counts['blocked_human'] += 1
                else:
                    counts['blocked_auto'] += 1
            if it in _ROLLUP_ITEM_TYPES and st not in ('done', 'archived', 'cancelled'):
                counts['open_rollup'] += 1

        stalled = (
            counts['actionable'] == 0
            and counts['open_rollup'] > 0
            and counts['done'] > 0
        )
        if not stalled:
            summary['skipped'] += 1
            reason = (
                'plan_complete' if counts['open_rollup'] == 0
                else ('never_started' if counts['done'] == 0 else 'has_actionable')
            )
            summary['skip_reasons'][reason] = summary['skip_reasons'].get(reason, 0) + 1
            continue

        # Idempotency: skip if there's an open plan-check task for this repo,
        # or if we nudged within the cooldown window.
        try:
            pres = es_search('agent-task-records', {
                'size': 1,
                'query': {'bool': {
                    'must': [
                        {'term': {'repo': repo_id}},
                        {'prefix': {'id': _PLAN_CHECK_PREFIX}},
                    ],
                    'should': [
                        {'bool': {'must_not': [{'term': {'status': 'done'}}]}},
                        {'range': {'updated_at': {'gte': cooldown_iso}}},
                    ],
                    'minimum_should_match': 1,
                }},
            })
            if (pres.get('hits', {}).get('hits') or []):
                summary['skipped'] += 1
                summary['skip_reasons']['recent_plan_check'] = summary['skip_reasons'].get('recent_plan_check', 0) + 1
                continue
        except Exception as e:
            logger.warning(json.dumps({
                'event': 'plan_progress.idempotency_check_failed',
                'repo': repo_id, 'error': str(e)[:200],
            }))
            # Proceed — worst case we create a duplicate, which is recoverable.

        plan_check_id = f'{_PLAN_CHECK_PREFIX}{repo_id}-{int(time.time())}'
        now = _now_iso()
        objective = (
            'The work queue has drained but the plan still has open epics/features/stories. '
            f'Project: {proj.get("name") or repo_id}. '
            f'Open rollup items: {counts["open_rollup"]}, done tasks: {counts["done"]}, '
            f'blocked (auto): {counts["blocked_auto"]}, blocked (human): {counts["blocked_human"]}.\n\n'
            'Inspect the project\'s acceptance criteria vs. merged commits and either:\n'
            '  1. Close out the remaining rollups if the work is actually complete, or\n'
            '  2. Emit concrete follow-up task(s) that will close the remaining gaps.\n'
            '\nCall implementation_complete with a clear summary of your decision.'
        )
        doc = {
            'id': plan_check_id,
            'title': f'Plan health check — {proj.get("name") or repo_id}',
            'objective': objective,
            'repo': repo_id,
            'item_type': 'task',
            'owner': 'pm-dispatcher',
            'assigned_agent_role': 'pm-dispatcher',
            'status': 'ready',
            'priority': 'high',
            'depends_on': [],
            'acceptance_criteria': [],
            'artifacts': [],
            'needs_human': False,
            'risk': 'low',
            'requires_code': False,
            'plan_check_signal': counts,
            'created_at': now,
            'updated_at': now,
            'last_update': now,
        }
        try:
            es_upsert('agent-task-records', plan_check_id, doc)
            summary['nudged'] += 1
            summary['nudged_ids'].append(plan_check_id)
            logger.info(json.dumps({
                'event': 'plan_progress.nudged',
                'repo': repo_id,
                'plan_check_id': plan_check_id,
                'counts': counts,
            }))
        except Exception as e:
            summary['errors'] += 1
            logger.warning(json.dumps({
                'event': 'plan_progress.upsert_failed',
                'repo': repo_id, 'error': str(e)[:300],
            }))

    return summary


# ---- scheduler / loop -------------------------------------------------------


def _record_summary(sweep_name: str, summary: dict, duration_ms: int) -> None:
    key_map = {
        'parent_revival': ('revived_total', 'revived', None),
        'stuck_worker_watchdog': ('recovered_total', 'recovered', 'escalated'),
        'plan_progress_scan': ('nudged_total', 'nudged', None),
    }
    with _STATE_LOCK:
        st = _STATE['sweeps'].get(sweep_name)
        if st is None:
            return
        st['last_run_at'] = _now_iso()
        st['last_duration_ms'] = duration_ms
        st['last_summary'] = summary
        st['runs'] += 1
        st['errors_total'] += int(summary.get('errors', 0))
        totals_key, primary_key, secondary_key = key_map.get(sweep_name, (None, None, None))
        if totals_key:
            st[totals_key] += int(summary.get(primary_key, 0))
        if secondary_key:
            st[f'{secondary_key}_total'] += int(summary.get(secondary_key, 0))


def _loop(deps: dict) -> None:
    logger = deps['logger']
    last_run_at = {
        'parent_revival': 0.0,
        'stuck_worker_watchdog': 0.0,
        'plan_progress_scan': 0.0,
    }

    # initial splay so sweeps don't all fire in the same second
    time.sleep(10)

    while True:
        now = time.time()
        parent_interval = _env_int('FLUME_PARENT_REVIVAL_INTERVAL_SEC', 90)
        stuck_interval = _env_int('FLUME_STUCK_TASK_INTERVAL_SEC', 120)
        plan_interval = _env_int('FLUME_PLAN_SCAN_INTERVAL_SEC', 600)

        if now - last_run_at['parent_revival'] >= parent_interval:
            started = time.time()
            try:
                summary = _parent_revival_sweep(deps)
            except Exception as e:
                summary = {'errors': 1}
                logger.exception(f'parent_revival.crashed: {e}')
                with _STATE_LOCK:
                    _STATE['sweeps']['parent_revival']['last_error'] = str(e)[:300]
            duration_ms = int((time.time() - started) * 1000)
            _record_summary('parent_revival', summary, duration_ms)
            if summary.get('revived') or summary.get('errors'):
                logger.info(json.dumps({'event': 'parent_revival.sweep', **summary, 'duration_ms': duration_ms}))
            last_run_at['parent_revival'] = time.time()

        if now - last_run_at['stuck_worker_watchdog'] >= stuck_interval:
            started = time.time()
            try:
                summary = _stuck_worker_watchdog(deps)
            except Exception as e:
                summary = {'errors': 1}
                logger.exception(f'stuck_worker.crashed: {e}')
                with _STATE_LOCK:
                    _STATE['sweeps']['stuck_worker_watchdog']['last_error'] = str(e)[:300]
            duration_ms = int((time.time() - started) * 1000)
            _record_summary('stuck_worker_watchdog', summary, duration_ms)
            if summary.get('recovered') or summary.get('escalated') or summary.get('errors'):
                logger.info(json.dumps({'event': 'stuck_worker.sweep', **summary, 'duration_ms': duration_ms}))
            last_run_at['stuck_worker_watchdog'] = time.time()

        if now - last_run_at['plan_progress_scan'] >= plan_interval:
            started = time.time()
            try:
                summary = _plan_progress_scan(deps)
            except Exception as e:
                summary = {'errors': 1}
                logger.exception(f'plan_progress.crashed: {e}')
                with _STATE_LOCK:
                    _STATE['sweeps']['plan_progress_scan']['last_error'] = str(e)[:300]
            duration_ms = int((time.time() - started) * 1000)
            _record_summary('plan_progress_scan', summary, duration_ms)
            if summary.get('nudged') or summary.get('errors'):
                logger.info(json.dumps({'event': 'plan_progress.sweep', **summary, 'duration_ms': duration_ms}))
            last_run_at['plan_progress_scan'] = time.time()

        tick = _env_int('FLUME_AUTONOMY_INTERVAL_SEC', 60)
        time.sleep(max(15, tick))


# ---- public API -------------------------------------------------------------


def maybe_start(
    *,
    es_search: Callable[[str, dict], dict],
    es_post: Callable[[str, dict], Any],
    es_upsert: Callable[[str, str, dict], Any],
    append_note: Callable[[str, str], bool],
    list_projects: Callable[[], list],
    logger,
) -> bool:
    """Idempotent start hook called from the dashboard lifespan."""
    with _STATE_LOCK:
        if _STATE['thread_alive']:
            return True
        enabled = _env_bool('FLUME_AUTONOMY_ENABLED', True)
        _STATE['enabled'] = enabled
        _STATE['config'] = {
            'loop_interval_sec': _env_int('FLUME_AUTONOMY_INTERVAL_SEC', 60),
            'parent_revival': {
                'interval_sec': _env_int('FLUME_PARENT_REVIVAL_INTERVAL_SEC', 90),
                'lookback_min': _env_int('FLUME_PARENT_REVIVAL_LOOKBACK_MIN', 180),
                'max_per_tick': _env_int('FLUME_PARENT_REVIVAL_MAX', 25),
            },
            'stuck_worker_watchdog': {
                'interval_sec': _env_int('FLUME_STUCK_TASK_INTERVAL_SEC', 120),
                'idle_minutes': _env_int('FLUME_STUCK_TASK_MINUTES', 25),
                'retry_cap': _env_int('FLUME_STUCK_TASK_RETRY_CAP', 3),
                'max_per_tick': _env_int('FLUME_STUCK_TASK_MAX', 25),
            },
            'plan_progress_scan': {
                'interval_sec': _env_int('FLUME_PLAN_SCAN_INTERVAL_SEC', 600),
                'cooldown_min': _env_int('FLUME_PLAN_SCAN_COOLDOWN_MIN', 60),
            },
        }
        if not enabled:
            logger.info('autonomy_sweeps: disabled via FLUME_AUTONOMY_ENABLED=0')
            return False
        _STATE['thread_alive'] = True

    deps = {
        'es_search': es_search,
        'es_post': es_post,
        'es_upsert': es_upsert,
        'append_note': append_note,
        'list_projects': list_projects,
        'logger': logger,
    }
    t = threading.Thread(target=_loop, args=(deps,), daemon=True, name='flume-autonomy')
    t.start()
    logger.info(json.dumps({'event': 'autonomy_sweeps.started', 'config': _STATE['config']}))
    return True


def get_status() -> dict:
    with _STATE_LOCK:
        # Return a deep-ish copy so callers can't mutate module state.
        return json.loads(json.dumps(_STATE))


def run_sweep_now(
    sweep_name: str,
    *,
    es_search: Callable[[str, dict], dict],
    es_post: Callable[[str, dict], Any],
    append_note: Callable[[str, str], bool],
    logger,
    es_upsert: Callable[[str, str, dict], Any] | None = None,
    list_projects: Callable[[], list] | None = None,
) -> dict:
    """Trigger a single sweep synchronously. Returns the summary."""
    deps = {
        'es_search': es_search,
        'es_post': es_post,
        'es_upsert': es_upsert,
        'append_note': append_note,
        'list_projects': list_projects,
        'logger': logger,
    }
    fn_map = {
        'parent_revival': _parent_revival_sweep,
        'stuck_worker_watchdog': _stuck_worker_watchdog,
        'plan_progress_scan': _plan_progress_scan,
    }
    fn = fn_map.get(sweep_name)
    if not fn:
        raise ValueError(f'unknown sweep: {sweep_name!r}')
    started = time.time()
    summary = fn(deps)
    duration_ms = int((time.time() - started) * 1000)
    _record_summary(sweep_name, summary, duration_ms)
    return {'sweep': sweep_name, 'duration_ms': duration_ms, 'summary': summary}
