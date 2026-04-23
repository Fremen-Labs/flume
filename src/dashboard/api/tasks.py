from datetime import datetime, timezone
import json
import urllib.parse
import subprocess
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from utils.logger import get_logger
from core.elasticsearch import es_search, es_post
from core.projects_store import PROJECTS_INDEX, _es_projects_request

from core.elasticsearch import find_task_doc_by_logical_id
from core.tasks import task_history, get_task_doc

def _lazy_is_remote_url(url: str) -> bool:
    from server import _is_remote_url as _inner
    return _inner(url)

logger = get_logger(__name__)
router = APIRouter()

@router.get("/api/tasks/{task_id}/history")
def api_task_history(task_id: str):
    data = task_history(task_id)
    if not data:
        return JSONResponse(status_code=404, content={'error': 'Task not found'})
    return data

@router.get("/api/tasks/{task_id}/diff")
def api_task_diff(task_id: str):
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
                pass

        clone_status = proj.get("clone_status") or proj.get("cloneStatus") or ""
        repo_url = proj.get("repoUrl") or ""

        # ── Remote repo: GitHostClient REST API ──────────────────────────────
        if clone_status in ("indexed", "cloned") and _lazy_is_remote_url(repo_url):
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

        # ── Local repo: git subprocess (clone_status='local') ────────────────
        local_path_str = proj.get("path") or task.get("worktree")
        if not local_path_str or not Path(local_path_str).exists():
            return {"diff": "", "error": "Repository not available locally; configure a PAT to enable API-based diff."}

        try:
            ref_out = subprocess.run(
                ["git", "-C", local_path_str, "symbolic-ref", "refs/remotes/origin/HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            base = ref_out.stdout.strip().split("/")[-1] if ref_out.returncode == 0 else "main"
        except Exception:
            base = "main"

        diff_out = subprocess.run(
            ["git", "-C", local_path_str, "diff", f"origin/{base}...{branch}"],
            capture_output=True, text=True, timeout=15,
        )
        diff_text = diff_out.stdout if diff_out.returncode == 0 else ""
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
def api_task_commits(task_id: str):
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
                pass

        clone_status = proj.get("clone_status") or proj.get("cloneStatus") or ""
        repo_url = proj.get("repoUrl") or ""

        if branch and clone_status in ("indexed", "cloned") and _lazy_is_remote_url(repo_url):
            try:
                client = get_git_client(proj)
                base = (
                    proj.get("gitflow", {}).get("defaultBranch")
                    or client.get_default_branch()
                )
                return client.get_commits(branch=branch, base=base)
            except GitHostError:
                pass
    except Exception:
        pass
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
def api_task_transition(task_id: str, payload: dict):
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
    status = (payload.get('status') or '').strip().lower()
    if status not in _ALLOWED_USER_STATUSES:
        return JSONResponse(
            status_code=400,
            content={'error': f'status must be one of {sorted(_ALLOWED_USER_STATUSES)}, got {status!r}'},
        )

    es_id, src = find_task_doc_by_logical_id(task_id)
    if not es_id or src is None:
        return JSONResponse(status_code=404, content={'error': f'task {task_id!r} not found'})

    instruction = (payload.get('instruction') or '').strip()
    if len(instruction) > 8000:
        return JSONResponse(
            status_code=400,
            content={'error': 'instruction exceeds 8000 characters'},
        )

    prev_status = (src.get('status') or '').strip().lower()
    auto_recovery = payload.get('auto_recovery_prompt', True)
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
def api_tasks_bulk_requeue(payload: dict):
    """
    Requeue up to 50 blocked tasks in one call.

    Body:    { "task_ids": ["story-1", "feat-1", ...] }
    Returns: { "requeued": [...], "failed": [...] }
    """
    _MAX_BULK = 50
    task_ids = payload.get('task_ids') or []
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


