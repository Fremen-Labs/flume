"""
Auto-unblocker daemon.

Scans `agent-task-records` for tasks in status=blocked that the user has NOT
pinned to human (needs_human != True) and tries to get them moving again by:

  1. Gathering the last few agent_log/execution_thoughts entries, the most
     recent failure record, handoff, and reviewer verdict.
  2. Asking the configured LLM for a concise recovery plan (≤ ~1200 chars).
     On LLM failure, falls back to a canned recovery hint — we still re-queue
     so the agent can re-try with fresh context.
  3. Appending the plan as an `[Auto-recovery]` note on agent_log.
  4. Flipping the task back to `status=ready` with the same owner it held
     when it blocked, clearing active_worker/queue_state so it is re-claimable.

Each attempt increments `auto_unblock_attempts`. Once a per-task cap is hit,
the task is marked `needs_human=True` with an explanatory note so it stops
being touched by the daemon and shows up prominently in the UI.

Configuration (env):

  FLUME_AUTO_UNBLOCK_ENABLED       default "1"  — master switch
  FLUME_AUTO_UNBLOCK_INTERVAL_SEC  default 180  — sweep period
  FLUME_AUTO_UNBLOCK_GRACE_SEC     default 120  — don't touch tasks that
                                                   just transitioned to blocked
  FLUME_AUTO_UNBLOCK_MAX           default 3    — attempts before escalation
  FLUME_AUTO_UNBLOCK_BATCH         default 10   — max tasks processed per sweep
  FLUME_AUTO_UNBLOCK_LLM_TIMEOUT   default 45   — seconds per LLM call

The module is deliberately self-contained: no FastAPI imports, no direct DB
writes outside of the helpers it receives from the dashboard server module.
"""

from __future__ import annotations

import json
import urllib.error
import httpx
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

# Module-level state used by the observability endpoint.
_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    'enabled': False,
    'running': False,
    'thread_alive': False,
    'last_sweep_at': None,
    'last_sweep_duration_ms': None,
    'last_sweep_summary': None,
    'sweep_count': 0,
    'unblocked_total': 0,
    'escalated_total': 0,
    'errors_total': 0,
    'last_error': None,
    'config': {},
}

_SKIP_ITEM_TYPES = {'epic', 'feature', 'story'}  # rollups unblock when children do


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, '').strip() or default)
    except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError):
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
    except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError):
        return None


def _tail(items: list, n: int) -> list:
    if not isinstance(items, list):
        return []
    return items[-n:] if len(items) > n else list(items)


def _clip(text: str, limit: int) -> str:
    if not text:
        return ''
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f'… [+{len(text) - limit} chars]'


def _should_skip(src: dict, grace_sec: int, max_attempts: int) -> str | None:
    """Return a reason string if this task should be skipped, else None."""
    if (src.get('status') or '').lower() != 'blocked':
        return 'not_blocked'
    if src.get('needs_human'):
        return 'needs_human'
    it = (src.get('item_type') or '').lower()
    if it in _SKIP_ITEM_TYPES:
        # Rollups should flow via compute_ready_for_repo when children finish.
        return f'rollup_item_type={it}'
    # Defer merge-conflict blocks to the pr_reconcile sweep. Re-queueing a
    # task that is blocked purely because its PR can't merge just loops —
    # the reviewer will re-approve identical code and the auto-merge will
    # fail again. Only pr_reconcile can actually rebase/resolve the branch,
    # so let it own these. Once the conflict is resolved upstream the flags
    # are cleared and this rule no longer skips.
    if src.get('merge_conflict') is True:
        return 'merge_conflict_pending_reconcile'
    attempts = int(src.get('auto_unblock_attempts') or 0)
    if attempts >= max_attempts:
        return 'max_attempts_reached'
    # Honor grace period after most recent transition.
    last_ts = _parse_iso(src.get('updated_at') or src.get('last_update') or '')
    if last_ts is not None and (time.time() - last_ts) < grace_sec:
        return 'within_grace_window'
    return None


def _collect_context(src: dict, es_search: Callable[[str, dict], dict]) -> dict:
    """Pull signal from related ES indices — best-effort, never raises."""
    task_id = src.get('id') or ''
    ctx: dict[str, Any] = {
        'agent_log_tail': _tail(src.get('agent_log') or [], 6),
        'execution_thoughts_tail': _tail(src.get('execution_thoughts') or [], 4),
        'last_failure': None,
        'last_handoff': None,
        'last_review': None,
    }
    # Recent failure
    try:
        res = es_search('agent-failure-records', {
            'size': 1,
            'sort': [{'created_at': {'order': 'desc'}}],
            'query': {'term': {'task_id': task_id}},
        })
        hits = res.get('hits', {}).get('hits', [])
        if hits:
            ctx['last_failure'] = hits[0].get('_source') or {}
    except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError):
        pass
    # Most recent handoff
    try:
        res = es_search('agent-handoff-records', {
            'size': 1,
            'sort': [{'created_at': {'order': 'desc'}}],
            'query': {'term': {'task_id': task_id}},
        })
        hits = res.get('hits', {}).get('hits', [])
        if hits:
            ctx['last_handoff'] = hits[0].get('_source') or {}
    except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError):
        pass
    # Most recent review
    try:
        res = es_search('agent-review-records', {
            'size': 1,
            'sort': [{'created_at': {'order': 'desc'}}],
            'query': {'term': {'task_id': task_id}},
        })
        hits = res.get('hits', {}).get('hits', [])
        if hits:
            ctx['last_review'] = hits[0].get('_source') or {}
    except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError):
        pass
    return ctx


_SYSTEM_PROMPT = (
    'You are a senior engineer triaging a blocked automation task. '
    'Given the task JSON and recent signals, write a short, actionable '
    'recovery plan the implementer agent can immediately follow. '
    'Keep it under 900 characters. '
    'Use 2–5 terse bullets. '
    'Be specific: point to the failing check, the file/function to inspect, '
    'and the next verification step (command to run, test to add, etc.). '
    'Do NOT restate the whole task. Do NOT apologise. Do NOT say "consult humans".'
)


def _build_user_prompt(src: dict, ctx: dict) -> str:
    slim_task = {
        'id': src.get('id'),
        'item_type': src.get('item_type'),
        'title': src.get('title'),
        'objective': _clip(src.get('objective') or '', 600),
        'acceptance_criteria': src.get('acceptance_criteria') or [],
        'owner': src.get('owner') or src.get('assigned_agent_role'),
        'branch': src.get('branch'),
        'commit_sha': src.get('commit_sha'),
        'attempts': src.get('auto_unblock_attempts') or 0,
    }
    slim_ctx = {
        'agent_log_tail': ctx.get('agent_log_tail') or [],
        'execution_thoughts_tail': [
            _clip(x.get('content') if isinstance(x, dict) else str(x), 400)
            for x in (ctx.get('execution_thoughts_tail') or [])
        ],
        'last_failure': {
            k: _clip(str(v), 400)
            for k, v in (ctx.get('last_failure') or {}).items()
            if k in ('summary', 'root_cause', 'error_class', 'fix_applied')
        },
        'last_handoff': {
            k: _clip(str(v), 300)
            for k, v in (ctx.get('last_handoff') or {}).items()
            if k in ('from_role', 'to_role', 'reason', 'status_hint')
        },
        'last_review': {
            k: _clip(str(v), 300)
            for k, v in (ctx.get('last_review') or {}).items()
            if k in ('verdict', 'summary', 'issues')
        },
    }
    return (
        'TASK:\n'
        + json.dumps(slim_task, ensure_ascii=False)
        + '\n\nSIGNALS:\n'
        + json.dumps(slim_ctx, ensure_ascii=False)
        + '\n\nRespond with ONLY the plan (no preamble).'
    )


def _llm_recovery_plan(src: dict, ctx: dict, timeout_seconds: int) -> tuple[str, bool]:
    """
    Returns (plan, llm_ok). On any LLM failure returns a canned plan + False.
    """
    canned = (
        'Re-queued automatically after a blocked state.\n'
        '- Read the last entries in agent_log and execution_thoughts above '
        'and identify the concrete failure.\n'
        '- Fix the root cause rather than the symptom; touch only the files '
        'named in the failure/handoff.\n'
        '- Re-run the relevant tests locally before handing off; add a '
        'regression test if one is missing.\n'
        '- If the root cause is truly external (missing secret, flaky network), '
        'say so explicitly in your final message so the next attempt is cheaper.'
    )
    try:
        from utils import llm_client  # lazy: imports settings
    except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError):
        return canned, False

    prompt_user = _build_user_prompt(src, ctx)
    try:
        reply = llm_client.chat(
            messages=[
                {'role': 'system', 'content': _SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt_user},
            ],
            temperature=0.2,
            max_tokens=512,
            timeout_seconds=timeout_seconds,
            agent_role='auto_unblocker',
        )
    except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError) as e:
        return canned + f'\n\n(LLM unavailable: {str(e)[:120]})', False

    plan = (reply or '').strip()
    if not plan:
        return canned, False
    # Safety clamp — should rarely trigger given max_tokens=512.
    if len(plan) > 1800:
        plan = plan[:1800] + '… [truncated]'
    return plan, True


def _requeue_task(
    *,
    es_id: str,
    src: dict,
    plan: str,
    llm_ok: bool,
    es_post: Callable[[str, dict], Any],
    append_note: Callable[[str, str], bool],
    logger,
) -> None:
    attempts = int(src.get('auto_unblock_attempts') or 0) + 1
    header = f'[Auto-recovery #{attempts}]' + ('' if llm_ok else ' (LLM fallback)')
    append_note(es_id, f'{header}\n{plan}')

    now = _now_iso()
    owner = src.get('owner') or src.get('assigned_agent_role')
    doc = {
        'status': 'ready',
        'queue_state': 'queued',
        'active_worker': None,
        'needs_human': False,
        'auto_unblock_attempts': attempts,
        'auto_unblock_last_at': now,
        'updated_at': now,
        'last_update': now,
        'implementer_consecutive_llm_failures': 0,
    }
    if owner:
        doc['owner'] = owner
        doc['assigned_agent_role'] = owner
    es_post(f'agent-task-records/_update/{es_id}', {'doc': doc})
    logger.info(json.dumps({
        'event': 'auto_unblock.requeued',
        'task_id': src.get('id'),
        'attempts': attempts,
        'llm_ok': llm_ok,
        'owner': owner,
    }))


def _escalate_task(
    *,
    es_id: str,
    src: dict,
    max_attempts: int,
    es_post: Callable[[str, dict], Any],
    append_note: Callable[[str, str], bool],
    logger,
) -> None:
    note = (
        f'[Auto-recovery] Giving up after {max_attempts} automatic attempts. '
        'Needs human guidance — use the Unblock dialog to provide direction '
        'or mark the task done/archived.'
    )
    append_note(es_id, note)
    now = _now_iso()
    doc = {
        'needs_human': True,
        'auto_unblock_last_at': now,
        'updated_at': now,
        'last_update': now,
    }
    es_post(f'agent-task-records/_update/{es_id}', {'doc': doc})
    logger.info(json.dumps({
        'event': 'auto_unblock.escalated',
        'task_id': src.get('id'),
        'attempts': int(src.get('auto_unblock_attempts') or 0),
    }))


def _sweep_once(deps: dict) -> dict:
    """
    Execute a single sweep. `deps` is the wiring from the dashboard server:
        {
          'es_search': es_search,
          'es_post':   es_post,
          'append_note': _append_task_agent_log_note,
          'logger':     logger,
        }
    Returns a summary dict suitable for logging / /api/auto-unblock/status.
    """
    logger = deps['logger']
    es_search = deps['es_search']
    es_post = deps['es_post']
    append_note = deps['append_note']

    grace = _env_int('FLUME_AUTO_UNBLOCK_GRACE_SEC', 120)
    max_attempts = _env_int('FLUME_AUTO_UNBLOCK_MAX', 3)
    batch = _env_int('FLUME_AUTO_UNBLOCK_BATCH', 10)
    llm_timeout = _env_int('FLUME_AUTO_UNBLOCK_LLM_TIMEOUT', 45)

    summary = {
        'scanned': 0, 'skipped': 0, 'unblocked': 0, 'escalated': 0, 'errors': 0,
        'skip_reasons': {},
        'ids_unblocked': [],
        'ids_escalated': [],
    }

    try:
        res = es_search('agent-task-records', {
            'size': 200,
            'sort': [{'updated_at': {'order': 'asc'}}],
            'query': {
                'bool': {
                    'must': [{'term': {'status': 'blocked'}}],
                    'must_not': [
                        {'term': {'needs_human': True}},
                        {'term': {'status': 'archived'}},
                    ],
                }
            },
        })
    except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError) as e:
        logger.warning(json.dumps({'event': 'auto_unblock.query_failed', 'error': str(e)[:200]}))
        summary['errors'] += 1
        return summary

    hits = res.get('hits', {}).get('hits', []) or []
    summary['scanned'] = len(hits)

    processed = 0
    for h in hits:
        if processed >= batch:
            break
        es_id = h.get('_id')
        src = h.get('_source') or {}
        reason = _should_skip(src, grace_sec=grace, max_attempts=max_attempts)
        if reason == 'max_attempts_reached':
            try:
                _escalate_task(
                    es_id=es_id, src=src,
                    max_attempts=max_attempts,
                    es_post=es_post, append_note=append_note, logger=logger,
                )
                summary['escalated'] += 1
                summary['ids_escalated'].append(src.get('id'))
                processed += 1
            except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError) as e:
                summary['errors'] += 1
                logger.warning(json.dumps({
                    'event': 'auto_unblock.escalate_failed',
                    'task_id': src.get('id'),
                    'error': str(e)[:200],
                }))
            continue
        if reason:
            summary['skipped'] += 1
            summary['skip_reasons'][reason] = summary['skip_reasons'].get(reason, 0) + 1
            continue

        try:
            ctx = _collect_context(src, es_search)
            plan, llm_ok = _llm_recovery_plan(src, ctx, timeout_seconds=llm_timeout)
            _requeue_task(
                es_id=es_id, src=src, plan=plan, llm_ok=llm_ok,
                es_post=es_post, append_note=append_note, logger=logger,
            )
            summary['unblocked'] += 1
            summary['ids_unblocked'].append(src.get('id'))
            processed += 1
        except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError) as e:
            summary['errors'] += 1
            logger.warning(json.dumps({
                'event': 'auto_unblock.process_failed',
                'task_id': src.get('id'),
                'error': str(e)[:300],
            }))

    return summary


def _loop(deps: dict) -> None:
    logger = deps['logger']
    interval = _env_int('FLUME_AUTO_UNBLOCK_INTERVAL_SEC', 180)
    # Small initial delay so it never races with lifespan startup tasks.
    time.sleep(min(30, max(5, interval // 6)))
    while True:
        started = time.time()
        summary = {}
        try:
            summary = _sweep_once(deps)
        except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError) as e:
            with _STATE_LOCK:
                _STATE['errors_total'] += 1
                _STATE['last_error'] = str(e)[:300]
            logger.exception(f'auto_unblock.sweep_crashed: {e}')
        finally:
            duration_ms = int((time.time() - started) * 1000)
            with _STATE_LOCK:
                _STATE['running'] = False
                _STATE['last_sweep_at'] = _now_iso()
                _STATE['last_sweep_duration_ms'] = duration_ms
                _STATE['last_sweep_summary'] = summary or None
                _STATE['sweep_count'] += 1
                _STATE['unblocked_total'] += int(summary.get('unblocked', 0) if summary else 0)
                _STATE['escalated_total'] += int(summary.get('escalated', 0) if summary else 0)
                _STATE['errors_total'] += int(summary.get('errors', 0) if summary else 0)
            if summary and (summary.get('unblocked') or summary.get('escalated') or summary.get('errors')):
                logger.info(json.dumps({'event': 'auto_unblock.sweep', **summary, 'duration_ms': duration_ms}))

        # Re-read interval on each loop so it can be tuned without a restart.
        interval = _env_int('FLUME_AUTO_UNBLOCK_INTERVAL_SEC', 180)
        time.sleep(max(15, interval))


def maybe_start(
    *,
    es_search: Callable[[str, dict], dict],
    es_post: Callable[[str, dict], Any],
    append_note: Callable[[str, str], bool],
    logger,
) -> bool:
    """Start the daemon thread once. Idempotent — repeat calls are no-ops."""
    with _STATE_LOCK:
        if _STATE['thread_alive']:
            return True
        enabled = _env_bool('FLUME_AUTO_UNBLOCK_ENABLED', True)
        _STATE['enabled'] = enabled
        _STATE['config'] = {
            'interval_sec': _env_int('FLUME_AUTO_UNBLOCK_INTERVAL_SEC', 180),
            'grace_sec': _env_int('FLUME_AUTO_UNBLOCK_GRACE_SEC', 120),
            'max_attempts': _env_int('FLUME_AUTO_UNBLOCK_MAX', 3),
            'batch': _env_int('FLUME_AUTO_UNBLOCK_BATCH', 10),
            'llm_timeout_sec': _env_int('FLUME_AUTO_UNBLOCK_LLM_TIMEOUT', 45),
        }
        if not enabled:
            logger.info('auto_unblock: disabled via FLUME_AUTO_UNBLOCK_ENABLED=0')
            return False
        _STATE['thread_alive'] = True

    deps = {
        'es_search': es_search,
        'es_post': es_post,
        'append_note': append_note,
        'logger': logger,
    }
    t = threading.Thread(target=_loop, args=(deps,), daemon=True, name='flume-auto-unblock')
    t.start()
    logger.info(json.dumps({'event': 'auto_unblock.started', 'config': _STATE['config']}))
    return True


def get_status() -> dict:
    with _STATE_LOCK:
        return dict(_STATE)
