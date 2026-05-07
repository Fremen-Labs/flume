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
from worker_handlers import _implementer_clear_claim_fields

def handle_implementer_worker(task, es_id):
    if task.get('release_promotion_task'):
        return _handle_release_promotion_task(task, es_id)

    # Guarantee absolute node isolation immediately via Worktree Sandboxing
    branch, worktree_path = ensure_task_branch(task)
    if branch and not task.get('branch'):
        update_task_doc(es_id, {'branch': branch})
        task['branch'] = branch

    # Early-exit re-queue when the clone fails for a remote repo.
    # Without this guard the agent runs against an empty/missing worktree,
    # produces no changes, and the task later blocks in the tester/reviewer
    # no-commit gate.
    if branch is None and task.get('repo'):
        src = _get_project_source(task.get('repo')) or {}
        clone_status = src.get('clone_status') or src.get('cloneStatus') or ''
        if clone_status != 'local':  # only applies to remote repos
            _clone_fail_msg = (
                'Re-queued: git clone failed. Verify the ADO/GH token on the Security page and '
                'that the repository URL is accessible from the worker container.'
            )
            append_agent_note(es_id, _clone_fail_msg)
            append_execution_thought(es_id, f"*[System]* \u274c {_clone_fail_msg}")
            update_task_doc(es_id, {
                'status': 'ready',
                'owner': 'implementer',
                'assigned_agent_role': 'implementer',
                'needs_human': False,
                **_implementer_clear_claim_fields(),
            })
            log(f"implementer: task={task.get('id')} clone failed — re-queued")
            return True

    repo_path = worktree_path or str(load_project_repo_path(task.get('repo')))

    # Clear any stale notes from previous runs, then write live progress to ES
    try:
        es_request(f'/{TASK_INDEX}/_update/{es_id}', {'doc': {'agent_log': [], 'execution_thoughts': []}}, method='POST')
    except Exception:
        pass

    # Prevent previous failed runs from polluting later tasks.
    # Keep `-fd` so gitignore'd files (e.g. node_modules/) are not deleted.
    try:
        if repo_path:
            subprocess.run(['git', '-C', repo_path, 'checkout', '--', '.'], capture_output=True, text=True)
            subprocess.run(['git', '-C', repo_path, 'clean', '-fd'], capture_output=True, text=True)
    except Exception:
        # If cleanup fails, we still try to run the agent; later gating will detect real changes.
        pass

    def _on_progress(note: str) -> None:
        append_agent_note(es_id, note)
        append_execution_thought(es_id, f"*[System]* {note}")

    def _on_thought(thought: str) -> None:
        if thought:
            append_execution_thought(es_id, thought)

    # Hint to the agent and enforce worker-side gating.
    task['requires_code'] = task_requires_code(task)
    task_id = task.get('id', '')
    released = False

    try:
        result = asyncio.run(_run_with_client(run_implementer, task, repo_path=repo_path, on_progress=_on_progress, on_thought=_on_thought))
        implementer_model = task.get('preferred_model') or _get_active_llm_model()


        if result.metadata.get('source') == 'llm_no_response':
            _implementer_handle_llm_failure(es_id, task, task_id)
            released = True
            return True

        commit_message = result.metadata.get('commit_message') or f"Implement task: {task.get('title', task_id)}"
        commit_sha = ''
        # NOTE: do NOT shadow `branch` here — it was set by ensure_task_branch() above.
        # The previous `branch = None` here was the root cause of all push failures.
        has_changes = False
        agent_completed = result.metadata.get('source') == 'llm_agentic'

        if repo_path and agent_completed:
            # Check whether the agent actually modified any files before touching git
            status = subprocess.run(
                ['git', '-C', repo_path, 'status', '--porcelain'],
                capture_output=True, text=True,
            )
            has_changes = bool(status.stdout.strip())

            # Enforce "no branches for non-code tasks":
            # If the agent wrote files for a task that we believe should be
            # analysis-only, discard those changes and treat the task as
            # having written no code.
            if has_changes and not task_requires_code(task):
                subprocess.run(['git', '-C', repo_path, 'checkout', '--', '.'], capture_output=True, text=True)
                subprocess.run(['git', '-C', repo_path, 'clean', '-fd'], capture_output=True, text=True)
                has_changes = False

            if has_changes:
                # Code was written inside the physically isolated worktree natively
                if branch:
                    commit_sha = auto_commit_and_push(repo_path, branch, commit_message, task_id)
                    if commit_sha == 'conflict':
                        append_agent_note(
                            es_id,
                            f"Push to remote aborted due to merge conflict on branch `{branch}`. "
                            "Human or Orchestrator intervention required to resolve."
                        )
                        update_task_doc(es_id, {
                            'status': 'blocked',
                            'queue_state': 'merge_conflict',
                            'blocked_reason': f"Merge conflict on pre-push rebase for {branch}"
                        })
                        teardown_task_clone(worktree_path)
                        return True

                    if has_changes and not commit_sha:
                        # Push failed — surface the error in the agent log so it's visible in the UI
                        append_agent_note(
                            es_id,
                            f"Push to remote failed for branch `{branch}`. "
                            "Check ADO_TOKEN / GH_TOKEN env vars on the worker and review worker_handlers.log.",
                        )

            # Edge case: branch already had commits from a previous partial run
            if not commit_sha and branch and _branch_has_new_commits(repo_path, branch):
                existing_sha, existing_msg = get_latest_commit_sha(repo_path)
                commit_sha = existing_sha
                if not commit_message:
                    commit_message = existing_msg

            # If the agent *should* have written code (has_changes=True) but we couldn't
            # record it (commit_sha == ''), the push to remote failed.
            # Apply a retry cap: re-queue for transient failures, block with reason
            # surfaced to the Work Queue card only after 4 consecutive failures.
            if has_changes and not commit_sha:
                _MAX_PUSH_FAILURES = int(os.environ.get('FLUME_MAX_PUSH_FAILURES', '3'))
                prev_push_failures = int(task.get('push_failure_count', 0))
                next_push_failures = prev_push_failures + 1

                if next_push_failures > _MAX_PUSH_FAILURES:
                    host = task.get('execution_host', 'localhost')
                    append_agent_note(
                        es_id,
                        f'Blocked on Node {host}: git push failed {next_push_failures} consecutive times (cap={_MAX_PUSH_FAILURES}). '
                        'The most common cause is an expired or incorrect ADO/GH token. '
                        'Check the Security page → Vault connection, then reset this task to **ready** to retry.',
                    )
                    update_task_doc(es_id, {
                        'status': 'blocked',
                        'needs_human': True,
                        'owner': 'implementer',
                        'assigned_agent_role': 'implementer',
                        'push_failure_count': next_push_failures,
                        **_implementer_clear_claim_fields(),
                    })
                    log(f"implementer: task={task_id} push failed {next_push_failures} times — blocking for human attention")
                else:
                    append_agent_note(
                        es_id,
                        f'Re-queued: git push to remote failed (attempt {next_push_failures}/{_MAX_PUSH_FAILURES}). '
                        'Branch was committed locally. Will retry automatically.',
                    )
                    update_task_doc(es_id, {
                        'status': 'ready',
                        'owner': 'implementer',
                        'assigned_agent_role': 'implementer',
                        'push_failure_count': next_push_failures,
                        **_implementer_clear_claim_fields(),
                    })
                    log(f"implementer: task={task_id} push failed — re-queued (attempt {next_push_failures}/{_MAX_PUSH_FAILURES})")
                released = True
                return True

            if commit_sha and branch:
                update_task_doc(es_id, {'branch': branch, 'worktree': repo_path})

        # ── Path A: code was written and committed ────────────────────────────
        # Normal flow: send through tester → reviewer pipeline.
        if commit_sha:
            write_doc(HANDOFF_INDEX, {
                'task_id': task_id,
                'from_role': 'implementer',
                'to_role': 'tester',
                'reason': result.summary,
                'objective': task.get('objective', ''),
                'inputs': result.artifacts,
                'constraints': commit_message or '',
                'status_hint': 'review',
                'model_used': implementer_model,
                'commit_sha': commit_sha,
                'branch': branch or task.get('branch'),
                'created_at': now_iso(),
            })
            update_task_doc(es_id, {
                'status': 'review',
                'owner': 'tester',
                'assigned_agent_role': 'tester',
                'artifacts': result.artifacts or task.get('artifacts', []),
                'branch': branch or task.get('branch'),
                'worktree': repo_path or task.get('worktree'),
                'commit_sha': commit_sha,
                'commit_message': commit_message,
                'implementer_consecutive_llm_failures': 0,
                **_implementer_clear_claim_fields(),
            })
            log(f"implementer completed task={task_id} branch={branch} sha={commit_sha[:8] if commit_sha else 'n/a'} -> tester")
            released = True
            return True

        # ── Path B: agent completed but wrote no code ──────────────────────────
        # This is an analysis, exploration, or context task. Mark it done directly
        # — no branch, no tester/reviewer needed.
        if agent_completed and not has_changes:
            if task_requires_code(task):
                # This task *should* result in code edits, but the agent wrote nothing.
                # Treat as LLM error to block gracefully instead of looping indefinitely.
                _implementer_handle_llm_failure(es_id, task, task_id)
                log(f"implementer: task={task_id} expected code edits but wrote nothing; handled as LLM failure")
                released = True
                return True

            update_task_doc(es_id, {
                'status': 'done',
                'owner': 'implementer',
                'assigned_agent_role': 'implementer',
                'implementer_consecutive_llm_failures': 0,
                'context_summary': result.summary,
                **_implementer_clear_claim_fields(),
            })
            write_doc(HANDOFF_INDEX, {
                'task_id': task_id,
                'from_role': 'implementer',
                'to_role': 'done',
                'reason': result.summary,
                'objective': task.get('objective', ''),
                'inputs': result.artifacts,
                'constraints': 'non-code task — no commit required',
                'status_hint': 'done',
                'model_used': implementer_model,
                'created_at': now_iso(),
            })
            promoted = compute_ready_for_repo(task.get('repo'))
            log(f"implementer completed non-code task={task_id} (analysis/exploration) — marked done directly; promoted={promoted}")
            released = True
            return True

        # ── Path C: agent failed to complete at all ────────────────────────────
        # Fallback / LLM error — re-queue so it can be retried automatically.
        _implementer_handle_llm_failure(es_id, task, task_id)
        released = True
        return True

    except Exception as e:
        log(f"implementer: task={task_id} exception: {e}")
        return False
    finally:
        # AP-5C: Always clean up the ephemeral clone so /tmp doesn't grow unbounded.
        # Before teardown, dump the undocumented state into Elasticsearch for debugging.
        try:
            if worktree_path and Path(worktree_path).exists() and str(Path(worktree_path)).startswith(tempfile.gettempdir()):
                status_out = subprocess.run(['git', '-C', worktree_path, 'status'], capture_output=True, text=True, timeout=5).stdout
                if status_out.strip():
                    diff_out = subprocess.run(['git', '-C', worktree_path, 'diff'], capture_output=True, text=True, timeout=5).stdout
                    note = f"**Ephemeral Workspace Pre-teardown state:**\n\n```text\n{status_out.strip()}\n```\n"
                    if diff_out.strip():
                        note += f"```diff\n{diff_out.strip()[:4000]}\n```"
                    append_agent_note(es_id, note)
        except Exception as _ex:
            log(f"implementer: failed to capture ephemeral state for task={task_id}: {_ex}")

        # teardown_task_clone() is a no-op for local repos and non-tmp paths.
        teardown_task_clone(worktree_path)
        if not released:
            try:
                _, cur = fetch_task_doc(task_id)
                if cur and str(cur.get('status') or '') == 'running':
                    # ── Loop termination: cap implementer exceptions ──────────
                    # Without this, a task that reliably throws (missing binary
                    # like `go`, Path Traversal Halted, Kill Switch abort) gets
                    # released back to `ready` forever. Cap the abnormal exits
                    # and escalate to human when the implementer can't even
                    # finish its handler.
                    _MAX_IMPL_EXCEPTIONS = int(
                        os.environ.get('FLUME_MAX_IMPLEMENTER_EXCEPTIONS', '3')
                    )
                    prev_excs = int(cur.get('implementer_exception_count', 0) or 0)
                    next_excs = prev_excs + 1
                    if _MAX_IMPL_EXCEPTIONS > 0 and next_excs >= _MAX_IMPL_EXCEPTIONS:
                        append_agent_note(
                            es_id,
                            f'Blocked: implementer handler has exited abnormally '
                            f'{next_excs} times (cap={_MAX_IMPL_EXCEPTIONS}, '
                            'FLUME_MAX_IMPLEMENTER_EXCEPTIONS). Common causes: '
                            'missing binary (`go`, `node`), path traversal '
                            'attempt, kill-switch abort, or LLM loop. Inspect '
                            'worker logs for the latest traceback and reset '
                            'the task to **ready** once the root cause is fixed.',
                        )
                        update_task_doc(es_id, {
                            'status': 'blocked',
                            'needs_human': True,
                            'owner': 'implementer',
                            'assigned_agent_role': 'implementer',
                            'implementer_exception_count': next_excs,
                            **_implementer_clear_claim_fields(),
                        })
                        try:
                            write_doc(FAILURE_INDEX, {
                                'id': f"failure-implexc-{task_id}-{int(time.time())}",
                                'task_id': task_id,
                                'project': task.get('repo'),
                                'repo': task.get('repo'),
                                'error_class': 'implementer_exception_cap',
                                'summary': f'Implementer handler aborted {next_excs} times without completing.',
                                'root_cause': 'Repeated abnormal handler exit',
                                'fix_applied': 'Task blocked; needs_human=true',
                                'model_used': task.get('preferred_model', ''),
                                'confidence': 'high',
                                'recurrence_count': next_excs,
                                'created_at': now_iso(),
                                'updated_at': now_iso(),
                            })
                        except Exception:
                            pass
                        log(
                            f"implementer: task={task_id} blocked by exception cap "
                            f"({next_excs}/{_MAX_IMPL_EXCEPTIONS})"
                        )
                    else:
                        update_task_doc(es_id, {
                            'status': 'ready',
                            'owner': 'implementer',
                            'assigned_agent_role': 'implementer',
                            'needs_human': False,
                            'implementer_exception_count': next_excs,
                            **_implementer_clear_claim_fields(),
                        })
                        log(
                            f"implementer: released stuck running task={task_id} "
                            f"(handler did not finish normally; excs={next_excs}/"
                            f"{_MAX_IMPL_EXCEPTIONS})"
                        )
            except Exception as ex:
                log(f"implementer: finally guard failed task={task_id}: {ex}")
