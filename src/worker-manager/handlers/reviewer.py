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

def handle_reviewer_worker(task, es_id):
    # Terminal tasks should never run reviewer LLM again (avoids tight loops + wasted Ollama calls).
    st = (task.get('status') or '').strip().lower()
    if st in ('done', 'cancelled', 'archived'):
        log(f"reviewer: task={task.get('id')} already {st} — skipping reviewer run")
        update_task_doc(es_id, {**_implementer_clear_claim_fields()})
        return True

    # Gate: don't approve if no real commit was recorded by the implementer
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
        log(f"reviewer: task={task.get('id')} has no commit_sha — blocked; cleared claim so task stops re-looping")
        return True

    result = asyncio.run(_run_with_client(run_reviewer, task))
    reviewer_model = task.get('preferred_model') or _get_active_llm_model()

    verdict = result.verdict or 'approved'
    task_id = task.get('id')
    write_doc(REVIEW_INDEX, {
        'review_id': f"review-{task_id}-{int(time.time())}",
        'task_id': task_id,
        'verdict': verdict,
        'summary': result.summary,
        'issues': '',
        'recommended_next_role': 'implementer' if verdict == 'changes_requested' else '',
        'model_used': reviewer_model,
        'promotion_candidate': verdict == 'approved',
        'confidence': 'medium',
        'created_at': now_iso(),
    })
    if verdict == 'approved':
        update_task_doc(es_id, {
            'status': 'done',
            'owner': 'reviewer',
            # Reset all loop-termination counters on success so re-opened tasks
            # get a fresh budget instead of inheriting stale counts.
            'tester_reject_count': 0,
            'reviewer_rework_count': 0,
            'tester_retry_count': 0,
            'reviewer_block_count': 0,
            'stuck_recovery_count': 0,
            'implementer_consecutive_llm_failures': 0,
            'implementer_exception_count': 0,
            'push_failure_count': 0,
            'needs_human': False,
            **_implementer_clear_claim_fields(),
        })
        write_doc(PROVENANCE_INDEX, {
            'id': f"prov-{task_id}-{int(time.time())}",
            'task_id': task_id,
            'project': task.get('repo'),
            'repo': task.get('repo'),
            'agent_role': 'reviewer',
            'context_refs': [],
            'tool_calls': {},
            'artifacts': task.get('artifacts', []),
            'review_verdict': 'approved',
            'model_used': reviewer_model,
            'branch': task.get('branch'),
            'commit_sha': task.get('commit_sha'),
            'created_at': now_iso(),
        })
        promoted = compute_ready_for_repo(task.get('repo'))
        log(f"reviewer approved task={task_id}; promoted={promoted}")

        # Auto-PR if toggle is enabled for this project
        gitflow = load_project_gitflow(task.get('repo'))
        if gitflow.get('autoPrOnApprove', True) and task.get('branch'):
            # Check idempotency — don't create a second PR
            if not task.get('pr_url'):
                if _should_defer_auto_pr_until_story_complete(task):
                    append_agent_note(
                        es_id,
                        'Auto-PR deferred: other tasks under this story are still in progress. '
                        'Flume will open one PR when the last story task is approved '
                        '(FLUME_AUTO_PR_SCOPE=story).',
                    )
                    log(f"auto-PR deferred for task={task_id} — story still has open sibling tasks")
                else:
                    pr_url, pr_number, pr_error = create_pr_for_task(task, reviewer_model, es_id=es_id)
                    if pr_url:
                        tb = resolve_pr_base_branch(task.get('repo') or '')
                        _, src_after_pr = fetch_task_doc(task_id)
                        pr_patch = {
                            'pr_url': pr_url,
                            'pr_number': pr_number,
                            'target_branch': tb,
                        }
                        # create_pr_for_task → _maybe_auto_merge may have already merged into develop
                        # and set pr_status / remote_branch_deleted — do not overwrite with 'open'.
                        if not (src_after_pr and str(src_after_pr.get('pr_status') or '') == 'merged'):
                            pr_patch['pr_status'] = 'open'
                        update_task_doc(es_id, pr_patch)
                        _backfill_story_pr_to_sibling_tasks(task, pr_url, pr_number, tb)
                        write_doc(HANDOFF_INDEX, {
                            'task_id': task_id,
                            'from_role': 'reviewer',
                            'to_role': 'done',
                            'reason': f'PR created: {pr_url}',
                            'objective': task.get('objective', ''),
                            'inputs': [],
                            'constraints': f'branch={task.get("branch")} pr={pr_url}',
                            'status_hint': 'done',
                            'model_used': reviewer_model,
                            'created_at': now_iso(),
                        })
                        log(f"auto-PR created for task={task_id}: {pr_url}")
                    elif pr_error:
                        # Log failure but don't block task completion
                        write_doc(FAILURE_INDEX, {
                            'id': f"failure-pr-{task_id}-{int(time.time())}",
                            'task_id': task_id,
                            'project': task.get('repo'),
                            'repo': task.get('repo'),
                            'error_class': 'pr_creation_failed',
                            'summary': f'Auto-PR creation failed: {pr_error}',
                            'root_cause': pr_error,
                            'fix_applied': 'Task marked done; PR can be created manually.',
                            'model_used': reviewer_model,
                            'confidence': 'high',
                            'recurrence_count': 1,
                            'created_at': now_iso(),
                            'updated_at': now_iso(),
                        })
                        update_task_doc(es_id, {'pr_status': 'failed', 'pr_error': pr_error})
                        log(f"auto-PR failed for task={task_id}: {pr_error}")
            else:
                log(f"PR already exists for task={task_id}, skipping auto-PR")
        return True

    if verdict == 'changes_requested':
        # ── Loop termination: reviewer rework cap ─────────────────────────
        # Reviewer 'changes_requested' kicks the task back to the implementer.
        # Without a ceiling this can ping-pong forever (the language-mismatch
        # loop we observed sent 107 rejections through a single project).
        # Cap it; when exceeded, block the task for human inspection.
        _MAX_REVIEWER_REWORK = int(os.environ.get('FLUME_MAX_REVIEWER_REWORK', '3'))
        prev_rework = int(task.get('reviewer_rework_count', 0) or 0)
        next_rework = prev_rework + 1

        if _MAX_REVIEWER_REWORK > 0 and next_rework > _MAX_REVIEWER_REWORK:
            append_agent_note(
                es_id,
                f'Blocked: reviewer has requested changes {next_rework} times '
                f'(cap={_MAX_REVIEWER_REWORK}, FLUME_MAX_REVIEWER_REWORK). '
                'This usually means the implementer is producing the wrong '
                'kind of output (wrong language, mis-decomposed plan, missing '
                'tooling). Inspect the latest review notes, fix the root '
                'cause, and reset this task to **ready** or archive it. '
                'Counter resets on approval.',
            )
            update_task_doc(es_id, {
                'status': 'blocked',
                'needs_human': True,
                'owner': 'reviewer',
                'assigned_agent_role': 'reviewer',
                'reviewer_rework_count': next_rework,
                **_implementer_clear_claim_fields(),
            })
            write_doc(FAILURE_INDEX, {
                'id': f"failure-loopcap-review-{task_id}-{int(time.time())}",
                'task_id': task_id,
                'project': task.get('repo'),
                'repo': task.get('repo'),
                'error_class': 'loop_cap_reviewer',
                'summary': f'Reviewer rework cap exceeded: {next_rework}/{_MAX_REVIEWER_REWORK}. '
                           f'Latest verdict: {result.summary}',
                'root_cause': 'Runaway reviewer/implementer loop terminated by cap',
                'fix_applied': 'Task blocked; needs_human=true',
                'model_used': reviewer_model,
                'confidence': 'high',
                'recurrence_count': next_rework,
                'created_at': now_iso(),
                'updated_at': now_iso(),
            })
            log(
                f"reviewer: task={task_id} blocked by rework cap "
                f"({next_rework}/{_MAX_REVIEWER_REWORK})"
            )
            return True

        # B3: Clear claim fields so the task is re-claimable by another worker
        update_task_doc(es_id, {
            'status': 'ready',
            'owner': 'implementer',
            'assigned_agent_role': 'implementer',
            'reviewer_rework_count': next_rework,
            **_implementer_clear_claim_fields(),
        })
        write_doc(FAILURE_INDEX, {
            'id': f"failure-review-{task_id}-{int(time.time())}",
            'task_id': task_id,
            'project': task.get('repo'),
            'repo': task.get('repo'),
            'error_class': 'review_changes_requested',
            'summary': result.summary,
            'root_cause': 'Reviewer requested revisions',
            'fix_applied': '',
            'model_used': reviewer_model,
            'confidence': 'medium',
            'recurrence_count': next_rework,
            'created_at': now_iso(),
            'updated_at': now_iso(),
        })
        log(
            f"reviewer requested changes for task={task_id} "
            f"(rework={next_rework}/{_MAX_REVIEWER_REWORK})"
        )
        return True

    # Unknown verdict (e.g. hallucinated value not caught by agent_runner normalisation).
    # Re-queue to the reviewer for another attempt, rather than permanently blocking.
    # If the reviewer has already looped too many times, escalate to human.
    _REVIEWER_BLOCK_CAP = int(os.environ.get('FLUME_REVIEWER_BLOCK_CAP', '3'))
    prev_blocks = int(task.get('reviewer_block_count', 0))
    next_blocks = prev_blocks + 1

    if next_blocks >= _REVIEWER_BLOCK_CAP:
        host = task.get('execution_host', 'localhost')
        append_agent_note(
            es_id,
            f'Blocked on Node {host}: reviewer returned an unresolvable verdict {next_blocks} times '
            f'(cap={_REVIEWER_BLOCK_CAP}, FLUME_REVIEWER_BLOCK_CAP). '
            'Manually review and reset this task to **ready** or **done** after inspection.',
        )
        update_task_doc(es_id, {
            'status': 'blocked',
            'needs_human': True,
            'owner': 'reviewer',
            'reviewer_block_count': next_blocks,
        })
        log(f"reviewer blocked task={task_id} after {next_blocks} unresolvable verdict attempts")
    else:
        append_agent_note(
            es_id,
            f'Re-queuing to reviewer: unexpected verdict returned (attempt {next_blocks}/{_REVIEWER_BLOCK_CAP}). '
            'The reviewer will attempt to reach a valid conclusion.',
        )
        update_task_doc(es_id, {
            'status': 'review',
            'owner': 'reviewer',
            'assigned_agent_role': 'reviewer',
            'reviewer_block_count': next_blocks,
            **_implementer_clear_claim_fields(),
        })
        log(f"reviewer: unresolvable verdict for task={task_id} — re-queued to reviewer (attempt {next_blocks})")
    return True
