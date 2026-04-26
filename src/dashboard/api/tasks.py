from datetime import datetime, timezone
import json
import urllib.parse
from pathlib import Path

from fastapi import APIRouter
from api.models import TaskTransitionRequest, BulkRequeueRequest, BulkUpdateRequest
from fastapi.responses import JSONResponse

from utils.logger import get_logger
from utils.url_helpers import is_remote_url
from core.elasticsearch import es_post
from core.projects_store import PROJECTS_INDEX, _es_projects_request

from core.elasticsearch import find_task_doc_by_logical_id, es_delete_doc
from core.tasks import task_history, delete_task_branches
from utils.async_subprocess import run_cmd_async


logger = get_logger(__name__)
router = APIRouter()

@router.get("/api/tasks/{task_id}/history")
def api_task_history(task_id: str):
    data = task_history(task_id)
    if not data:
        return JSONResponse(status_code=404, content={'error': 'Task not found'})
    return data

@router.get("/api/tasks/{task_id}/diff")
async def api_task_diff(task_id: str):
    """
    Return the git diff for the branch associated with a task.
    AP-4B: Uses GitHostClient REST API for remote repos (no local clone required).
    Compares task branch against the repo's default branch.
    """
    from utils.git_host_client import get_git_client, GitHostError  # noqa

    try:
        es_id, task = find_task_doc_by_logical_id(task_id)
        if not task:
            return {"diff": "", "error": "Task not found"}

        branch = task.get("branch")
        if not branch:
            return {"diff": "", "error": "No branch recorded for this task"}

        repo_id = task.get("repo")
        proj = {}
        if repo_id:
            try:
                proj_res = _es_projects_request(f"/{PROJECTS_INDEX}/_doc/{repo_id}")
                proj = proj_res.get("_source") or {}
            except Exception:
                logger.debug("api_task_diff: failed to fetch project doc", exc_info=True)

        clone_status = proj.get("clone_status") or proj.get("cloneStatus") or ""
        repo_url = proj.get("repoUrl") or ""

        # ── Remote repo: GitHostClient REST API ──────────────────────────────
        if clone_status in ("indexed", "cloned") and is_remote_url(repo_url):
            try:
                client = get_git_client(proj)
                base = (
                    proj.get("gitflow", {}).get("defaultBranch")
                    or client.get_default_branch()
                )
                result = client.get_diff(base=base, head=branch)
                return {
                    "diff": result.get("diff", ""),
                    "branch": branch,
                    "base": base,
                    "files": result.get("files", []),
                    "truncated": result.get("truncated", False),
                }
            except GitHostError as e:
                return {"diff": "", "error": str(e)[:300]}

        # ── Local repo: async git subprocess (clone_status='local') ────────────────
        local_path_str = proj.get("path") or task.get("worktree")
        if not local_path_str or not Path(local_path_str).exists():
            return {"diff": "", "error": "Repository not available locally; configure a PAT to enable API-based diff."}

        try:
            rc, out, _err = await run_cmd_async(
                "git", "-C", local_path_str, "symbolic-ref", "refs/remotes/origin/HEAD", timeout=5
            )
            base = out.strip().split("/")[-1] if rc == 0 else "main"
        except Exception:
            base = "main"

        rc, out, _err = await run_cmd_async(
            "git", "-C", local_path_str, "diff", f"origin/{base}...{branch}", timeout=15
        )
        diff_text = out if rc == 0 else ""
        if len(diff_text) > 80_000:
            diff_text = diff_text[:80_000] + "\n\n... [diff truncated at 80k chars] ..."
        return {"diff": diff_text, "branch": branch, "base": f"origin/{base}"}
    except Exception as e:
        logger.warning({"event": "task_diff_error", "task_id": task_id, "error": str(e)})
        return {"diff": "", "error": str(e)}

@router.get("/api/tasks/{task_id}/thoughts")
def api_task_thoughts(task_id: str):
    _, source = find_task_doc_by_logical_id(task_id)
    if not source:
        return {"thoughts": []}
    return {"thoughts": source.get("execution_thoughts", [])}

@router.get("/api/tasks/{task_id}/commits")
async def api_task_commits(task_id: str):
    from utils.git_host_client import get_git_client, GitHostError  # noqa

    try:
        _, task = find_task_doc_by_logical_id(task_id)
        if not task:
            return []

        branch = task.get("branch")
        repo_id = task.get("repo")
        proj = {}
        if repo_id:
            try:
                proj_res = _es_projects_request(f"/{PROJECTS_INDEX}/_doc/{repo_id}")
                proj = proj_res.get("_source") or {}
            except Exception:
                logger.debug("api_task_commits: failed to fetch project doc", exc_info=True)

        clone_status = proj.get("clone_status") or proj.get("cloneStatus") or ""
        repo_url = proj.get("repoUrl") or ""

        if branch and clone_status in ("indexed", "cloned") and is_remote_url(repo_url):
            try:
                client = get_git_client(proj)
                base = (
                    proj.get("gitflow", {}).get("defaultBranch")
                    or client.get_default_branch()
                )
                return client.get_commits(branch=branch, base=base)
            except GitHostError as e:
                logger.debug(f"api_task_commits: GitHostError: {e}", exc_info=True)
    except Exception as e:
        logger.warning(f"api_task_commits: unexpected error: {e}", exc_info=True)
    return []

def _append_task_agent_log_note(es_id: str, note: str) -> bool:
    """
    Append one entry to task.agent_log (same shape as worker append_agent_note).
    Used for human guidance when unblocking from the dashboard.
    """
    note = (note or '').strip()
    if not note:
        return False
    ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    safe_id = urllib.parse.quote(str(es_id), safe='')
    try:
        es_post(
            f'agent-task-records/_update/{safe_id}',
            {
                'script': {
                    'source': (
                        'if (ctx._source.agent_log == null) { ctx._source.agent_log = []; }'
                        'ctx._source.agent_log.add(params.entry);'
                        'if (ctx._source.agent_log.length > 100) { ctx._source.agent_log.remove(0); }'
                        'ctx._source.updated_at = params.touch;'
                        'ctx._source.last_update = params.touch;'
                    ),
                    'lang': 'painless',
                    'params': {'entry': {'ts': ts, 'note': note}, 'touch': ts},
                },
            },
        )
        return True
    except Exception as e:
        logger.warning(json.dumps({'event': 'append_task_agent_log_failed', 'error': str(e)[:300]}))
        return False


@router.post("/api/tasks/{task_id}/transition")
def api_task_transition(task_id: str, payload: TaskTransitionRequest):
    """
    Transition a task to a new status.

    Allowed user-initiated transitions:
      - ready   → re-queues the task from the same role it blocked on
      - planned → demotes back to planning phase
      - inbox   → returns to the intake queue

    Optional:
      - instruction: human guidance appended to agent_log before transition (implementer sees full task JSON).
      - auto_recovery_prompt: when true (default) and transitioning blocked → ready without instruction,
        append a standard recovery hint so the agent re-tests and fixes root cause.

    History (agent_log, commit_sha, execution_thoughts) is preserved so
    engineers can read why the task blocked before retrying.
    """
    _ALLOWED_USER_STATUSES = {'ready', 'planned', 'inbox'}
    status = (payload.status or '').strip().lower()
    if status not in _ALLOWED_USER_STATUSES:
        return JSONResponse(
            status_code=400,
            content={'error': f'status must be one of {sorted(_ALLOWED_USER_STATUSES)}, got {status!r}'},
        )

    es_id, src = find_task_doc_by_logical_id(task_id)
    if not es_id or src is None:
        return JSONResponse(status_code=404, content={'error': f'task {task_id!r} not found'})

    instruction = (payload.instruction or '').strip()
    if len(instruction) > 8000:
        return JSONResponse(
            status_code=400,
            content={'error': 'instruction exceeds 8000 characters'},
        )

    prev_status = (src.get('status') or '').strip().lower()
    auto_recovery = payload.auto_recovery_prompt
    if isinstance(auto_recovery, str):
        auto_recovery = auto_recovery.strip().lower() not in ('0', 'false', 'no', 'off')

    if instruction:
        _append_task_agent_log_note(es_id, f'[Human guidance] {instruction}')
    elif status == 'ready' and prev_status == 'blocked' and auto_recovery:
        _append_task_agent_log_note(
            es_id,
            '[Recovery] Re-queued after blocked. Read prior agent_log and execution_thoughts; '
            'fix the root cause, run or add tests, and iterate until acceptance criteria are met.',
        )

    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    # Restart from the same role that was assigned when the task blocked.
    owner = src.get('owner') or src.get('assigned_agent_role')

    doc = {
        'status': status,
        'queue_state': 'queued',
        'active_worker': None,
        'needs_human': False,
        'updated_at': now,
        'last_update': now,
        'implementer_consecutive_llm_failures': 0,
    }
    if owner:
        doc['owner'] = owner
        doc['assigned_agent_role'] = owner

    es_post(f'agent-task-records/_update/{es_id}', {'doc': doc})
    logger.info(f'task transition: {task_id} → {status} (role={owner})')
    return {'success': True, 'task_id': task_id, 'status': status, 'owner': owner, '_id': es_id}


@router.post("/api/tasks/bulk-requeue")
def api_tasks_bulk_requeue(payload: BulkRequeueRequest):
    """
    Requeue up to 50 blocked tasks in one call.

    Body:    { "task_ids": ["story-1", "feat-1", ...] }
    Returns: { "requeued": [...], "failed": [...] }
    """
    _MAX_BULK = 50
    task_ids = payload.task_ids or []
    if not isinstance(task_ids, list):
        return JSONResponse(status_code=400, content={'error': 'task_ids must be a list'})
    if len(task_ids) > _MAX_BULK:
        return JSONResponse(status_code=400, content={'error': f'bulk limit is {_MAX_BULK} tasks per call'})

    requeued, failed = [], []
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    for task_id in task_ids:
        try:
            es_id, src = find_task_doc_by_logical_id(str(task_id))
            if not es_id or src is None:
                failed.append({'task_id': task_id, 'error': 'not found'})
                continue
            prev = (src.get('status') or '').strip().lower()
            if prev == 'blocked':
                _append_task_agent_log_note(
                    es_id,
                    '[Recovery] Bulk re-queue after blocked. Read prior agent_log and execution_thoughts; '
                    'fix root cause, run tests, and iterate until done.',
                )
            owner = src.get('owner') or src.get('assigned_agent_role')
            doc = {
                'queue_state': 'queued',
                'active_worker': None,
                'needs_human': False,
                'updated_at': now,
                'last_update': now,
                'implementer_consecutive_llm_failures': 0,
            }
            # Route the task to the status its role actually claims from:
            # pm -> planned, tester/reviewer -> review, everyone else -> ready.
            # Without this, owner=reviewer + status=ready silently orphans
            # the task (neither reviewer nor implementer worker will claim it).
            role = (owner or '').strip().lower() or 'implementer'
            if role not in ('implementer', 'tester', 'reviewer', 'pm', 'intake', 'memory-updater'):
                role = 'implementer'
            if role == 'pm':
                doc['status'] = 'planned'
            elif role in ('tester', 'reviewer'):
                doc['status'] = 'review'
            else:
                doc['status'] = 'ready'
            doc['owner'] = role
            doc['assigned_agent_role'] = role
            es_post(f'agent-task-records/_update/{es_id}', {'doc': doc})
            requeued.append({'task_id': task_id, 'owner': role, 'status': doc['status']})
        except Exception as exc:
            logger.error(f'bulk-requeue: task {task_id} failed: {exc}')
            failed.append({'task_id': task_id, 'error': str(exc)[:200]})

    logger.info(f'bulk-requeue: requeued={len(requeued)} failed={len(failed)}')
    return {'requeued': requeued, 'failed': failed}

@router.post('/api/tasks/bulk-update')
async def api_tasks_bulk_update(payload: BulkUpdateRequest):
    """
    Bulk archive or delete tasks from the project task list.

    Body: { "ids": ["task-1", ...], "action": "archive" | "delete", "repo": "<project id>" }
    When `repo` is set, tasks whose `repo` field does not match are skipped (failed).
    """
    _MAX_BULK = 200
    ids = payload.ids or []
    action = (payload.action or '').strip().lower()
    repo = (payload.repo or '').strip()

    if action not in ('archive', 'delete'):
        return JSONResponse(
            status_code=400,
            content={'error': f'action must be "archive" or "delete", got {action!r}'},
        )
    if not isinstance(ids, list):
        return JSONResponse(status_code=400, content={'error': 'ids must be a list'})
    if not ids:
        return JSONResponse(status_code=400, content={'error': 'ids must not be empty'})
    if len(ids) > _MAX_BULK:
        return JSONResponse(
            status_code=400,
            content={'error': f'bulk limit is {_MAX_BULK} tasks per call'},
        )

    str_ids = [str(i) for i in ids]
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    ok, failed = [], []

    if action == 'archive':
        for task_id in str_ids:
            try:
                es_id, src = find_task_doc_by_logical_id(task_id)
                if not es_id or src is None:
                    failed.append({'task_id': task_id, 'error': 'not found'})
                    continue
                logical_repo = (src.get('repo') or '').strip()
                if repo and logical_repo != repo:
                    failed.append({'task_id': task_id, 'error': 'repo mismatch'})
                    continue
                doc = {
                    'status': 'archived',
                    'active_worker': None,
                    'needs_human': False,
                    'updated_at': now,
                    'last_update': now,
                }
                es_post(f'agent-task-records/_update/{es_id}', {'doc': doc})
                ok.append({'task_id': task_id})
            except Exception as exc:
                logger.error(f'bulk-update archive: task {task_id} failed: {exc}')
                failed.append({'task_id': task_id, 'error': str(exc)[:200]})
        logger.info(f'bulk-update archive: ok={len(ok)} failed={len(failed)}')
        return {'archived': ok, 'failed': failed}

    # delete — clean up git branches while ES rows still exist, then remove docs
    try:
        await delete_task_branches(str_ids, repo)
    except Exception as exc:
        logger.warning(f'bulk-update delete: delete_task_branches: {exc}')

    for task_id in str_ids:
        try:
            es_id, src = find_task_doc_by_logical_id(task_id)
            if not es_id or src is None:
                failed.append({'task_id': task_id, 'error': 'not found'})
                continue
            logical_repo = (src.get('repo') or '').strip()
            if repo and logical_repo != repo:
                failed.append({'task_id': task_id, 'error': 'repo mismatch'})
                continue
            if es_delete_doc('agent-task-records', es_id):
                ok.append({'task_id': task_id})
            else:
                failed.append({'task_id': task_id, 'error': 'not found in index'})
        except Exception as exc:
            logger.error(f'bulk-update delete: task {task_id} failed: {exc}')
            failed.append({'task_id': task_id, 'error': str(exc)[:200]})

    logger.info(f'bulk-update delete: deleted={len(ok)} failed={len(failed)}')
    return {'deleted': ok, 'failed': failed}
