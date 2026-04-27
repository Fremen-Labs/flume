import os
import json
import time
import subprocess
import tempfile
import asyncio
from pathlib import Path
from worker_handlers import *
from agent_runner import (
    run_pm_dispatcher,
    run_implementer,
    run_tester,
    run_reviewer,
    _get_active_llm_model,
    _run_with_client
)

def handle_tester_worker(task, es_id):
    # Gate: refuse to test a task that has no real commit — nothing to validate
    if not task.get('commit_sha'):
        note = ('Blocked: no commit_sha recorded. The implementer did not push any code changes. '
                'Check worker_handlers.log for worktree/push errors, then reset this task to **ready**.')
        append_agent_note(es_id, note)
        update_task_doc(es_id, {
            'status': 'blocked',
            'needs_human': True,
            'owner': 'implementer',
            'assigned_agent_role': 'implementer',
            **_implementer_clear_claim_fields(),
        })
        log(f"tester: task={task.get('id')} has no commit_sha — blocked; cleared claim so task stops re-looping")
        return True

    # ── Tester retry loop cap ─────────────────────────────────────────────
    # Prevents infinite tester→reviewer loops when the reviewer never claims
    # (e.g. handoff bug, reviewer crash, or persistent LLM failures).
    # Mirrors the FLUME_REVIEWER_BLOCK_CAP pattern.
    _TESTER_RETRY_CAP = int(os.environ.get('FLUME_TESTER_RETRY_CAP', '5'))
    prev_retries = int(task.get('tester_retry_count', 0))
    next_retries = prev_retries + 1
    task_id = task.get('id')

    if _TESTER_RETRY_CAP > 0 and next_retries > _TESTER_RETRY_CAP:
        host = task.get('execution_host', 'localhost')
        append_agent_note(
            es_id,
            f'Blocked on Node {host}: tester has looped {next_retries} times without the reviewer completing '
            f'(cap={_TESTER_RETRY_CAP}, FLUME_TESTER_RETRY_CAP). '
            'This usually indicates a handoff or reviewer claim issue. '
            'Manually review and reset this task to **ready** or **done** after inspection.',
        )
        update_task_doc(es_id, {
            'status': 'blocked',
            'needs_human': True,
            'owner': 'tester',
            'tester_retry_count': next_retries,
            **_implementer_clear_claim_fields(),
        })
        log(f"tester: task={task_id} blocked after {next_retries} retry loops (cap={_TESTER_RETRY_CAP})")
        return True

    result = asyncio.run(_run_with_client(run_tester, task))
    tester_model = task.get('preferred_model') or _get_active_llm_model()

    if result.action == 'fail':
        # ── Loop termination: bug recursion depth + per-task rejection cap ──
        # Without these, a persistently-failing task can (a) spawn an unbounded
        # chain of bug-bug-bug-*-* follow-ups, and (b) re-queue the implementer
        # forever. Both are hard-capped here; when exceeded we block the task
        # with needs_human=true so a person can intervene instead of the
        # platform burning cycles.
        _MAX_BUG_DEPTH = int(os.environ.get('FLUME_MAX_BUG_DEPTH', '2'))
        _MAX_TESTER_REJECTIONS = int(os.environ.get('FLUME_MAX_TESTER_REJECTIONS', '3'))
        current_depth = _bug_recursion_depth(task.get('id') or '')
        prev_rejects = int(task.get('tester_reject_count', 0) or 0)
        next_rejects = prev_rejects + 1

        depth_exceeded = _MAX_BUG_DEPTH > 0 and current_depth >= _MAX_BUG_DEPTH
        rejects_exceeded = _MAX_TESTER_REJECTIONS > 0 and next_rejects > _MAX_TESTER_REJECTIONS

        if depth_exceeded or rejects_exceeded:
            reason_bits = []
            if depth_exceeded:
                reason_bits.append(
                    f'bug recursion depth={current_depth} >= cap {_MAX_BUG_DEPTH} '
                    '(FLUME_MAX_BUG_DEPTH)'
                )
            if rejects_exceeded:
                reason_bits.append(
                    f'tester has rejected this task {next_rejects} times '
                    f'(cap={_MAX_TESTER_REJECTIONS}, FLUME_MAX_TESTER_REJECTIONS)'
                )
            reason = ' and '.join(reason_bits)
            append_agent_note(
                es_id,
                'Blocked: ' + reason + '. '
                'No new bug task was created. Inspect the task, fix the '
                'underlying issue (e.g. wrong language, missing dependency, '
                'mis-decomposed plan), and reset this task to **ready** or '
                'archive it. Counters reset on approval.',
            )
            update_task_doc(es_id, {
                'status': 'blocked',
                'needs_human': True,
                'owner': 'tester',
                'assigned_agent_role': 'tester',
                'tester_reject_count': next_rejects,
                **_implementer_clear_claim_fields(),
            })
            write_doc(FAILURE_INDEX, {
                'id': f"failure-loopcap-{task.get('id')}-{int(time.time())}",
                'task_id': task.get('id'),
                'project': task.get('repo'),
                'repo': task.get('repo'),
                'error_class': 'loop_cap_tester',
                'summary': reason + ' — escalated to human.',
                'root_cause': 'Runaway tester/bug loop terminated by cap',
                'fix_applied': 'Task blocked; needs_human=true',
                'model_used': tester_model,
                'confidence': 'high',
                'recurrence_count': next_rejects,
                'created_at': now_iso(),
                'updated_at': now_iso(),
            })
            log(
                f"tester: task={task.get('id')} blocked by loop cap "
                f"(depth={current_depth}/{_MAX_BUG_DEPTH}, "
                f"rejects={next_rejects}/{_MAX_TESTER_REJECTIONS})"
            )
            return True

        bugs = result.bugs or [{
            'title': f"Bug follow-up for {task.get('title', task.get('id'))}",
            'objective': result.summary,
            'severity': 'high',
        }]
        for idx, bug in enumerate(bugs, start=1):
            create_bug_task(task, bug, idx)
        # B2: Clear claim fields so the task is re-claimable by another worker
        update_task_doc(es_id, {
            'status': 'ready',
            'owner': 'implementer',
            'assigned_agent_role': 'implementer',
            'tester_reject_count': next_rejects,
            **_implementer_clear_claim_fields(),
        })
        write_doc(FAILURE_INDEX, {
            'id': f"failure-{task.get('id')}-{int(time.time())}",
            'task_id': task.get('id'),
            'project': task.get('repo'),
            'repo': task.get('repo'),
            'error_class': 'test_failure',
            'summary': result.summary,
            'root_cause': 'Automated validation failed',
            'fix_applied': '',
            'model_used': tester_model,
            'confidence': 'medium',
            'recurrence_count': next_rejects,
            'created_at': now_iso(),
            'updated_at': now_iso(),
        })
        log(
            f"tester found bugs for task={task.get('id')} and re-queued implementer "
            f"(depth={current_depth}/{_MAX_BUG_DEPTH}, "
            f"rejects={next_rejects}/{_MAX_TESTER_REJECTIONS})"
        )
        return True

    write_doc(HANDOFF_INDEX, {
        'task_id': task_id,
        'from_role': 'tester',
        'to_role': 'reviewer',
        'reason': result.summary,
        'objective': task.get('objective', ''),
        'inputs': task.get('artifacts', []),
        'constraints': '',
        'status_hint': 'review',
        'model_used': tester_model,
        'created_at': now_iso(),
    })
    # FIX: Clear active_worker and queue_state so the reviewer's atomic claim
    # can pick up the task. Previously these fields were left set, causing
    # the Painless guard (active_worker == null) to noop every reviewer claim
    # and creating an infinite tester→reviewer loop.
    update_task_doc(es_id, {
        'status': 'review',
        'owner': 'reviewer',
        'assigned_agent_role': 'reviewer',
        'active_worker': None,
        'queue_state': 'queued',
        'tester_retry_count': next_retries,
    })
    log(f"tester passed task={task_id} -> reviewer (attempt {next_retries}/{_TESTER_RETRY_CAP if _TESTER_RETRY_CAP > 0 else '∞'})")
    return True
