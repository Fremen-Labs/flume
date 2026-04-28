#!/usr/bin/env python3
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from utils.es_auth import get_es_auth_headers

import socket
import httpx
from datetime import datetime, timezone
from pathlib import Path

NODE_ID = os.environ.get('HOSTNAME') or socket.gethostname() or "null-node"

# AP-10: Import ES-native model reader
try:
    import sys as _sys
    _wm_src = str(__import__('pathlib').Path(__file__).resolve().parent.parent)
    if _wm_src not in _sys.path:
        _sys.path.insert(0, _wm_src)
    from workspace_llm_env import get_active_llm_model as _get_active_llm_model
except Exception:
    def _get_active_llm_model(default: str = 'llama3.2') -> str:  # type: ignore[misc]
        return (os.environ.get('LLM_MODEL') or default).strip() or default


_WS = Path(__file__).resolve().parent.parent
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))
from flume_secrets import apply_runtime_config  # noqa: E402
from workspace_llm_env import sync_llm_env_from_workspace  # noqa: E402

apply_runtime_config(_WS)


async def _run_with_client(func, *args, **kwargs):
    async with httpx.AsyncClient() as client:
        kwargs['client'] = client
        return await func(*args, **kwargs)

BASE = _WS / 'worker-manager'
from utils.workspace import resolve_safe_workspace  # noqa: E402

ES_URL = os.environ.get('ES_URL', 'http://elasticsearch:9200').rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY', '')
ES_VERIFY_TLS = os.environ.get('ES_VERIFY_TLS', 'false').lower() == 'true'
TASK_INDEX = os.environ.get('ES_INDEX_TASKS', 'agent-task-records')
HANDOFF_INDEX = os.environ.get('ES_INDEX_HANDOFFS', 'agent-handoff-records')
FAILURE_INDEX = os.environ.get('ES_INDEX_FAILURES', 'agent-failure-records')
REVIEW_INDEX = os.environ.get('ES_INDEX_REVIEWS', 'agent-review-records')
PROVENANCE_INDEX = os.environ.get('ES_INDEX_PROVENANCE', 'agent-provenance-records')
POLL_SECONDS = int(os.environ.get('WORKER_MANAGER_POLL_SECONDS', '15'))
BACKOFF_BASE_DELAY = float(os.environ.get('FLUME_BACKOFF_BASE_DELAY', '2.0'))
BACKOFF_MAX_DELAY = float(os.environ.get('FLUME_BACKOFF_MAX_DELAY', '30.0'))
BACKOFF_JITTER_FACTOR = float(os.environ.get('FLUME_BACKOFF_JITTER_FACTOR', '0.2'))
# AP-3 cleanup: PROJECTS_REGISTRY (projects.json) removed — gitflow config read from ES flume-projects index.

ctx = None
if not ES_VERIFY_TLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# AP-6: file_path logger arg removed — get_logger writes to stdout only.
from utils.logger import get_logger  # noqa: E402
_handlers_logger = get_logger('worker-handlers')


def log(msg, **kwargs):
    if kwargs:
        _handlers_logger.info(str(msg), extra={'structured_data': kwargs})
    else:
        _handlers_logger.info(str(msg))


def es_request(path, body=None, method='GET'):
    headers = dict(get_es_auth_headers())
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
        if method == 'GET':
            method = 'POST'
    req = urllib.request.Request(f"{ES_URL}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, context=ctx) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def fetch_task_doc(task_id):
    """
    Resolve (es_id, source) by logical id. Matches dashboard server behavior: document _id
    is usually the logical id (PUT _doc/<id>), but dynamic mappings may require term /
    match_phrase on the `id` field.
    """
    tid = (task_id or '').strip()
    if not tid:
        return None, None
    for query in (
        {'ids': {'values': [tid]}},
        {'term': {'id': tid}},
        {'term': {'id.keyword': tid}},
        {'match_phrase': {'id': tid}},
    ):
        try:
            res = es_request(
                f'/{TASK_INDEX}/_search',
                {'size': 1, 'query': query},
                method='POST',
            )
            hits = res.get('hits', {}).get('hits', [])
            if hits:
                hit = hits[0]
                return hit.get('_id'), hit.get('_source', {})
        except Exception:
            continue
    return None, None


def emit_task_event(task_id: str, event_type: str, details: dict):
    """Event-Sourced State Machine: Append an immutable event describing a task transition (CQRS OpenHands pattern)."""
    # Sanitize details to prevent accidental token linkage
    sanitized = {
        k: v for k, v in details.items() 
        if not k.lower().endswith(('token', 'key', 'secret', 'password'))
    }
    event_doc = {
        'task_id': task_id,
        'event_type': event_type,
        'details': sanitized,
        'timestamp': now_iso()
    }
    try:
        es_request('/flume-task-events/_doc', event_doc, method='POST')
        _handlers_logger.debug(f"Event Sourcing: Emitted {event_type} for task {task_id}")
    except Exception as e:
        _handlers_logger.error(f"Event Sourcing Failure: Could not emit event for task {task_id}: {e}")

def log_task_state_transition(task_id: str, prev_status: str, new_status: str, role: str, worker_name: str, project: str = ""):
    """Emit a flat state transition event for the lifecycle observer."""
    event = {
        'task_id': task_id,
        'previous_status': prev_status,
        'new_status': new_status,
        'role': role,
        'worker_name': worker_name,
        'owner': role,
        'project': project,
        'timestamp': now_iso()
    }
    try:
        es_request('/flume-task-events/_doc', body=event, method='POST')
        _handlers_logger.debug(f"Lifecycle Event: Task {task_id} transitioned from {prev_status} to {new_status}")
    except Exception as e:
        _handlers_logger.error(f"Lifecycle Event Failure: Could not emit transition for task {task_id}: {e}")


class KillSwitchAbortError(Exception):
    pass


def check_kill_switch(es_id: str):
    """Enforce native state bounding to synchronously interrupt stray execution loops."""
    try:
        res = es_request(f'/{TASK_INDEX}/_doc/{es_id}?_source=status,repo,owner,assigned_agent_role,active_worker')
        src = res.get('_source', {})
        if src.get('status') == 'blocked':
            _handlers_logger.warning("Kill Switch Engaged: Worker thread aborting immediately for blocked task.")
            raise KillSwitchAbortError("Task was halted via Kill Switch")
        return src
    except KillSwitchAbortError as e:
        raise e
    except Exception as e:
        _handlers_logger.debug(f"Non-fatal failure checking kill switch for {es_id}: {e}")
        return {}


def update_task_doc(es_id, doc):
    old_src = check_kill_switch(es_id) or {}
    
    old_status = old_src.get('status')
    new_status = doc.get('status')
    
    if new_status and old_status and new_status != old_status:
        from lifecycle.state_machine import TaskStateMachine, InvalidTransitionError
        try:
            TaskStateMachine.transition(es_id, old_status, new_status, repo=old_src.get('repo', ''))
        except InvalidTransitionError as e:
            _handlers_logger.error(f"FSM Blocked Invalid Transition for {es_id}: {e}")
            raise e

    doc['updated_at'] = now_iso()
    doc['last_update'] = now_iso()
    
    # Dual-Write CQRS Materialization: Emit immutable event before updating in-place Materialized View
    emit_task_event(es_id, 'doc_update', doc)
    
    es_request(f'/{TASK_INDEX}/_update/{es_id}', {'doc': doc}, method='POST')
    
    if new_status and old_status and new_status != old_status:
        role = doc.get('assigned_agent_role') or old_src.get('assigned_agent_role') or ''
        worker = doc.get('active_worker') or old_src.get('active_worker') or ''
        repo = old_src.get('repo', '')
        log_task_state_transition(es_id, old_status, new_status, role, worker, repo)


def write_doc(index, doc):
    es_request(f'/{index}/_doc', doc, method='POST')


def append_agent_note(es_id: str, note: str) -> None:
    """Append a live progress note to the task's agent_log field (capped at 100 entries)."""
    check_kill_switch(es_id)
    try:
        ts = now_iso()
        es_request(f'/{TASK_INDEX}/_update/{es_id}', {
            'script': {
                'id': 'flume-append-agent-note',
                'params': {'entry': {'ts': ts, 'note': note}, 'touch': ts},
            }
        }, method='POST')
    except Exception:
        pass


def append_execution_thought(es_id: str, thought: str) -> None:
    """Append a live thought to the task's execution_thoughts field (capped at 500 entries)."""
    check_kill_switch(es_id)
    try:
        ts = now_iso()
        es_request(f'/{TASK_INDEX}/_update/{es_id}', {
            'script': {
                'id': 'flume-append-execution-thought',
                'params': {'entry': {'ts': ts, 'thought': thought}, 'touch': ts},
            }
        }, method='POST')
    except Exception as e:
        log(f"[worker_handlers] append_execution_thought error: {e}")


def _es_projects_request_worker(path: str, body=None, method: str = "GET") -> dict:
    """Lightweight ES request helper scoped to flume-projects index (no httpx dep)."""
    headers = {"Content-Type": "application/json"}
    headers.update(get_es_auth_headers())
    data = json.dumps(body).encode() if body is not None else None
    if data and method == "GET":
        method = "POST"
    _ctx = None
    if not ES_VERIFY_TLS:
        _ctx = ssl.create_default_context()
        _ctx.check_hostname = False
        _ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(f"{ES_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=_ctx) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def load_project_repo_path(repo_id):
    """
    AP-4B / AP-5C: Legacy helper — now only used for 'local' clone_status projects
    (user-provided filesystem paths). Remote repos use ephemeral clones via
    ensure_task_branch(); this function returns None for them.

    Queries the flume-projects ES index. Returns a Path only when clone_status is
    'local' and the directory actually exists on disk.
    """
    if not repo_id:
        return None
    try:
        src = _get_project_source(repo_id)
        if not src:
            return None

        clone_status = src.get("clone_status") or src.get("cloneStatus") or ""

        if clone_status == "cloning":
            log(f"load_project_repo_path: repo={repo_id} is still cloning — worker should re-queue")
            return None

        # For 'local' projects, the user supplied a local path; honour it.
        if clone_status == "local":
            local_path = src.get("path") or src.get("localPath")
            if local_path and Path(local_path).exists():
                return Path(local_path)

        # Remote repos (indexed/cloned) no longer retain a persistent local path.
        # Workers use ensure_task_branch() to get an ephemeral clone instead.
        return None
    except Exception as e:
        log(f"load_project_repo_path: ES lookup failed for repo={repo_id}: {e}")
        return None


def _get_project_source(repo_id: str) -> dict:
    """
    Fetch the flume-projects ES document source for a repo ID.
    Returns {} on missing or error.
    """
    if not repo_id:
        return {}
    try:
        res = _es_projects_request_worker(f"/flume-projects/_doc/{repo_id}")
        return res.get("_source") or {}
    except Exception as e:
        log(f"_get_project_source: ES lookup failed for repo={repo_id}: {e}")
        return {}


def _build_auth_clone_url(repo_url: str, repo_id: str) -> str:
    """
    Build a credential-embedded clone URL for a remote repository.

    Credential resolution priority (same as api_create_project in server.py):
      1. OpenBao KV via ado_tokens_store / github_tokens_store
      2. ADO_TOKEN / ADO_PERSONAL_ACCESS_TOKEN env vars
      3. GH_TOKEN / GITHUB_TOKEN env vars

    Returns the authenticated URL, or the original URL when no PAT is available
    (git will fail with auth error, which is the correct behaviour).
    """
    from utils.git_credentials import detect_repo_type, strip_credentials, _rewrite_url  # noqa
    if not repo_url:
        return ""
    repo_type = detect_repo_type(repo_url)
    clean_url = strip_credentials(repo_url)

    pat = ""
    ws = None
    try:
        from utils.workspace import resolve_safe_workspace  # noqa
        ws = resolve_safe_workspace()
    except Exception:
        pass

    if repo_type == "ado":
        if ws:
            try:
                import ado_tokens_store  # noqa
                raw = ado_tokens_store.get_active_token_plain(ws)
                if raw and "OPENBAO_DELEGATED" not in raw:
                    pat = raw
            except Exception:
                pass
        if not pat:
            pat = (
                os.environ.get("ADO_TOKEN", "").strip()
                or os.environ.get("ADO_PERSONAL_ACCESS_TOKEN", "").strip()
            )
    elif repo_type == "github":
        if ws:
            try:
                import github_tokens_store  # noqa
                raw = github_tokens_store.get_active_token_plain(ws)
                if raw and "OPENBAO_DELEGATED" not in raw:
                    pat = raw
            except Exception:
                pass
        if not pat:
            pat = (
                os.environ.get("GH_TOKEN", "").strip()
                or os.environ.get("GITHUB_TOKEN", "").strip()
            )

    if pat:
        return _rewrite_url(clean_url, pat)
    return clean_url


def _flume_ensure_integration_branch_enabled() -> bool:
    """When true (default), create the integration branch on the remote if missing before branching."""
    v = os.environ.get("FLUME_ENSURE_INTEGRATION_BRANCH", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _maybe_ensure_integration_branch(
    repo_id: str,
    src: dict,
    *,
    local_repo_path: Path | None = None,
) -> None:
    """
    Ensure gitflow integration branch (usually ``develop``) exists at the default-branch tip
    so new feature branches stack on it. Uses Git host API first, then a local ``git push``
    fallback when a local repo path is provided.
    """
    if not repo_id or not _flume_ensure_integration_branch_enabled():
        return
    gf = load_project_gitflow(repo_id)
    if not gf.get("ensureIntegrationBranch", True):
        return
    ib = (gf.get("integrationBranch") or "develop").strip()
    if not ib:
        return
    try:
        from utils.git_host_client import ensure_integration_branch_for_project  # noqa: PLC0415

        if ensure_integration_branch_for_project(src, ib):
            log(f"ensure_integration_branch: API ensured {ib!r} for repo={repo_id}")
    except Exception as e:
        log(f"ensure_integration_branch: API failed repo={repo_id}: {e}")

    if local_repo_path and (local_repo_path / ".git").exists():
        _ensure_local_integration_branch_push(local_repo_path, ib)


def _ensure_local_integration_branch_push(repo_path: Path, integration_branch: str) -> None:
    """If ``origin/<integration_branch>`` is missing, create it from the remote default branch tip."""
    ib = (integration_branch or "").strip()
    if not ib:
        return
    try:
        subprocess.run(
            ["git", "-C", str(repo_path), "fetch", "origin"],
            capture_output=True,
            timeout=120,
        )
        chk = subprocess.run(
            ["git", "-C", str(repo_path), "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{ib}"],
            capture_output=True,
        )
        if chk.returncode == 0:
            return
        db = resolve_default_branch(str(repo_path))
        if ib == db:
            return
        pr = subprocess.run(
            ["git", "-C", str(repo_path), "push", "origin", f"refs/remotes/origin/{db}:refs/heads/{ib}"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if pr.returncode != 0:
            log(
                f"ensure_local_integration_branch: push {db!r} -> {ib!r} failed: "
                f"{(pr.stderr or pr.stdout or '')[:300]}"
            )
        else:
            log(f"ensure_local_integration_branch: created origin/{ib} from origin/{db}")
    except Exception as e:
        log(f"ensure_local_integration_branch: {e}")


def _configure_git_identity(repo_path: Path) -> None:
    """Set a bot git identity if not already configured (required in containers)."""
    for cfg_key, cfg_val in [
        ("user.email", "ai-bot@flume.local"),
        ("user.name",  "Flume AI Bot"),
    ]:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "config", cfg_key],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            subprocess.run(
                ["git", "-C", str(repo_path), "config", cfg_key, cfg_val],
                capture_output=True,
            )


def _flume_branch_scope() -> str:
    """task = legacy per-leaf branch; story = one shared branch per story (long-running work)."""
    return os.environ.get('FLUME_BRANCH_SCOPE', 'story').strip().lower() or 'story'


def _flume_auto_pr_scope() -> str:
    """task = open PR when each task is approved; story = one PR when the last task under the story completes."""
    return os.environ.get('FLUME_AUTO_PR_SCOPE', 'task').strip().lower() or 'task'


def _sanitize_git_branch_segment(s: str) -> str:
    out = re.sub(r'[^a-zA-Z0-9._-]+', '-', (s or '').strip())
    return (out.strip('-')[:80] or 'scope')


def _stable_scope_hash(scope_id: str) -> str:
    return hashlib.sha256(scope_id.encode('utf-8')).hexdigest()[:6]


def _resolve_branch_scope_id(task: dict) -> str | None:
    """
    Return a stable grouping id for branch naming (usually parent story id).
    None => fall back to per-task branch (legacy).
    """
    it = (task.get('item_type') or '').lower()
    pid = task.get('parent_id')
    if not pid:
        return None
    ps = str(pid)
    if it == 'task' and ps.startswith('story-'):
        return ps
    if it == 'bug' and ps.startswith('story-'):
        return ps
    return None


def _iter_repo_tasks_for_repo(repo_id: str):
    """Yield (es_id, _source) for non-archived tasks in a repo."""
    hits = []
    if repo_id:
        try:
            res = es_request(
                f'/{TASK_INDEX}/_search',
                {'size': 500, 'query': {'bool': {'must_not': [{'term': {'status': 'archived'}}]}}},
                method='POST',
            )
            hits = res.get('hits', {}).get('hits', []) or []
        except Exception:
            hits = []
    for h in hits:
        src = h.get('_source') or {}
        if src.get('repo') != repo_id:
            continue
        yield h.get('_id'), src


def _fetch_repo_task_map(repo_id: str) -> dict[str, dict]:
    """In-memory map id -> task _source for one repo (same pattern as compute_ready_for_repo)."""
    out: dict[str, dict] = {}
    for _es_id, src in _iter_repo_tasks_for_repo(repo_id):
        tid = src.get('id')
        if tid:
            out[str(tid)] = src
    return out


def _should_defer_auto_pr_until_story_complete(task: dict) -> bool:
    """
    When FLUME_AUTO_PR_SCOPE=story, skip opening a PR until every task under the
    same story has reached a terminal state, so one branch can accumulate commits.
    """
    if _flume_auto_pr_scope() != 'story':
        return False
    if (task.get('item_type') or '').lower() != 'task':
        return False
    story_id = task.get('parent_id')
    if not story_id or not str(story_id).startswith('story-'):
        return False
    repo_id = task.get('repo')
    by_id = _fetch_repo_task_map(repo_id)
    siblings = [
        t for t in by_id.values()
        if t.get('parent_id') == story_id and (t.get('item_type') or '').lower() == 'task'
    ]
    if len(siblings) <= 1:
        return False

    def _terminal(x: dict) -> bool:
        if not x:
            return False
        if x.get('status') in ('done', 'archived'):
            return True
        if x.get('queue_state') == 'skipped':
            return True
        return False

    this_id = task.get('id')
    for s in siblings:
        sid = s.get('id')
        if sid == this_id:
            continue  # current task is being approved now — treat as terminal
        if not _terminal(s):
            log(
                f"auto_pr: defer PR for task={this_id} — sibling {sid} not terminal "
                f"(status={s.get('status')})"
            )
            return True
    return False


def _backfill_story_pr_to_sibling_tasks(
    task: dict, pr_url: str, pr_number: int | None, target_branch: str,
) -> None:
    """Copy pr_url to all tasks on the same story branch when opening one story-level PR."""
    if _flume_auto_pr_scope() != 'story':
        return
    repo_id = task.get('repo')
    story_id = task.get('parent_id')
    branch = task.get('branch')
    if not repo_id or not story_id or not branch:
        return
    if not str(story_id).startswith('story-'):
        return
    try:
        for es_id, src in _iter_repo_tasks_for_repo(repo_id):
            if src.get('parent_id') != story_id:
                continue
            if (src.get('item_type') or '').lower() != 'task':
                continue
            if src.get('branch') != branch:
                continue
            if src.get('pr_url'):
                continue
            update_task_doc(es_id, {
                'pr_url': pr_url,
                'pr_number': pr_number,
                'pr_status': 'open',
                'target_branch': target_branch,
            })
    except Exception as e:
        log(f"backfill story PR metadata failed: {e}")


def ensure_task_branch(task: dict) -> tuple[str | None, str | None]:
    """
    AP-5C: Ephemeral shallow clone — K8s-native task isolation.

    Replaces the previous git-worktree approach with a per-task tempdir clone.
    Each call produces an independent working copy that:
      - Lives in /tmp/flume-<task_id>-*/  (ephemeral, pod-scoped)
      - Contains NO .git/worktrees/ metadata shared with other pods
      - Is deleted after the task completes via teardown_task_clone()

    For 'local' clone_status projects the previous local-path behaviour is
    preserved for developer ergonomics (no ephemeral clone for local repos).

    Returns (branch_name, worktree_path) or (None, None) on failure.
    """
    task_id  = task.get('id') or 'task'
    repo_id  = task.get('repo')

    if task.get('branch'):
        branch = task.get('branch')
    else:
        item_type = (task.get('item_type') or '').lower()
        prefix    = 'bugfix' if (item_type == 'bug' or task_id.startswith('bug-')) else 'feature'
        scope_id = _resolve_branch_scope_id(task) if _flume_branch_scope() == 'story' else None
        if scope_id:
            seg = _sanitize_git_branch_segment(scope_id)
            h = _stable_scope_hash(scope_id)
            branch = f"{prefix}/{seg}-{h}"
        else:
            # Stable per-task branch: reusing the same name across retries lets the
            # implementer stack commits on prior work and prevents the orphan-branch
            # explosion we saw when every retry generated a fresh uuid suffix.
            safe_tid = _sanitize_git_branch_segment(task_id)
            
            # Incorporate the task title into the branch name for human context
            title = task.get('title') or ''
            if title:
                # Sanitize title aggressively, take first 50 chars to avoid absurdly long branches
                safe_title = _sanitize_git_branch_segment(title.lower()[:50])
                branch = f"{prefix}/{safe_tid}-{safe_title}"
            else:
                branch = f"{prefix}/{safe_tid}"

    # ── Local repo path: retain legacy worktree-free behaviour ──────────────
    # For locally-mounted repos (clone_status='local') we still check out a
    # branch directly in the repo without creating worktrees.
    src = _get_project_source(repo_id) or {}
    clone_status = src.get("clone_status") or src.get("cloneStatus") or ""

    if clone_status == "local":
        local_path_str = src.get("path") or src.get("localPath")
        if local_path_str and Path(local_path_str).exists():
            repo_path = Path(local_path_str)
            if (repo_path / ".git").exists():
                try:
                    _maybe_ensure_integration_branch(repo_id, src, local_repo_path=repo_path)
                    proc = subprocess.run(
                        ["git", "-C", str(repo_path), "show-ref", "--verify", "--quiet",
                         f"refs/heads/{branch}"],
                        capture_output=True,
                    )
                    if proc.returncode == 0:
                        subprocess.run(
                            ["git", "-C", str(repo_path), "checkout", branch],
                            check=True, capture_output=True,
                        )
                    else:
                        gf = load_project_gitflow(repo_id)
                        ib = (gf.get("integrationBranch") or "develop").strip()
                        subprocess.run(
                            ["git", "-C", str(repo_path), "fetch", "origin"],
                            capture_output=True,
                            timeout=120,
                        )
                        ib_ok = subprocess.run(
                            ["git", "-C", str(repo_path), "show-ref", "--verify", "--quiet",
                             f"refs/remotes/origin/{ib}"],
                            capture_output=True,
                        )
                        integration_base = f"origin/{ib}" if ib_ok.returncode == 0 else None
                        if integration_base:
                            subprocess.run(
                                ["git", "-C", str(repo_path), "checkout", "-b", branch, integration_base],
                                check=True, capture_output=True,
                            )
                        else:
                            subprocess.run(
                                ["git", "-C", str(repo_path), "checkout", "-b", branch],
                                check=True, capture_output=True,
                            )
                    return branch, str(repo_path)
                except Exception as e:
                    log(f"ensure_task_branch: local checkout failed task={task_id}: {e}")
                    return None, str(repo_path)
        log(f"ensure_task_branch: local repo path missing for repo={repo_id}")
        return None, None

    # ── Remote repo: ephemeral shallow clone ─────────────────────────────────
    repo_url = src.get("repoUrl") or src.get("repo_url") or ""
    if not repo_url:
        log(f"ensure_task_branch: no repoUrl for repo={repo_id}; cannot clone")
        return None, None

    _maybe_ensure_integration_branch(repo_id, src)

    auth_url = _build_auth_clone_url(repo_url, repo_id)

    # Configurable clone depth: shallow is fast; 50 commits is enough for
    # git log on the feature branch when checking _branch_has_new_commits.
    clone_depth = int(os.environ.get("FLUME_CLONE_DEPTH", "50"))

    tmp = Path(tempfile.mkdtemp(prefix=f"flume-{task_id}-"))
    try:
        log(f"ensure_task_branch: cloning repo={repo_id} depth={clone_depth} into {tmp}")
        subprocess.run(
            [
                "git", "clone",
                f"--depth={clone_depth}",
                "--no-tags",
                "--single-branch",
                "--",
                auth_url,
                str(tmp),
            ],
            check=True, capture_output=True, timeout=300,
        )
        _configure_git_identity(tmp)

        # Expand history slightly so _branch_has_new_commits() can compare
        # against origin/<default> without "fatal: unrelated histories"
        try:
            subprocess.run(
                ["git", "-C", str(tmp), "fetch", "--depth=50", "origin"],
                capture_output=True, timeout=60,
            )
        except Exception:
            pass  # best-effort shallow unshallow

        gf = load_project_gitflow(repo_id)
        ib = (gf.get('integrationBranch') or 'develop').strip()
        try:
            subprocess.run(
                ["git", "-C", str(tmp), "fetch", "--depth=50", "origin", ib],
                capture_output=True, timeout=90,
            )
        except Exception:
            pass
        ib_ref = f"refs/remotes/origin/{ib}"
        ib_ok = subprocess.run(
            ["git", "-C", str(tmp), "show-ref", "--verify", "--quiet", ib_ref],
            capture_output=True,
        ).returncode == 0
        if ib_ok:
            integration_base = f"origin/{ib}"
        else:
            integration_base = "HEAD"
            log(
                f"ensure_task_branch: integration branch origin/{ib} not found — "
                f"branching from default HEAD (create {ib} on remote for gitflow)"
            )

        # Check whether the task branch already exists remotely (crash recovery)
        remote_branch_check = subprocess.run(
            ["git", "-C", str(tmp), "ls-remote", "--heads", "origin", branch],
            capture_output=True, text=True, timeout=20,
        )
        remote_exists = bool(remote_branch_check.stdout.strip())

        if remote_exists:
            # Previous run pushed commits; fetch the branch with enough depth
            # so its tip commit is available (--single-branch clone only has main).
            subprocess.run(
                ["git", "-C", str(tmp), "fetch", f"--depth={clone_depth}", "origin", branch],
                check=True, capture_output=True, timeout=60,
            )
            # Use FETCH_HEAD — guaranteed to point to the fetched branch tip.
            # origin/{branch} may not resolve because --single-branch limits remote tracking.
            subprocess.run(
                ["git", "-C", str(tmp), "checkout", "-b", branch, "FETCH_HEAD"],
                check=True, capture_output=True,
            )
            log(f"ensure_task_branch: resumed existing remote branch={branch} for task={task_id}")
        else:
            subprocess.run(
                ["git", "-C", str(tmp), "checkout", "-b", branch, integration_base],
                check=True, capture_output=True,
            )
            log(
                f"ensure_task_branch: created new branch={branch} from {integration_base} "
                f"for task={task_id}"
            )

        return branch, str(tmp)

    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace")[:300] if e.stderr else str(e)
        log(f"ensure_task_branch: clone failed task={task_id}: {stderr}")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None
    except Exception as e:
        log(f"ensure_task_branch: unexpected error task={task_id}: {e}")
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None


def teardown_task_clone(worktree_path: str | None) -> None:
    """
    AP-5C: Remove the ephemeral clone directory created by ensure_task_branch.
    Called in the finally block of handle_implementer_worker() after all git
    operations (commit, push, PR) are complete.

    Safe to call with None or a path that no longer exists.
    Does NOT remove local repo paths (clone_status='local').
    """
    if not worktree_path:
        return
    p = Path(worktree_path)
    # Only auto-delete paths that look like our temp dirs to avoid accidents
    if not str(p).startswith(tempfile.gettempdir()):
        return
    if p.exists():
        try:
            shutil.rmtree(p)
            log(f"teardown_task_clone: removed ephemeral clone at {p}")
        except Exception as e:
            log(f"teardown_task_clone: failed to remove {p}: {e}")


def get_latest_commit_sha(repo_path):
    """Return the latest commit SHA and message on HEAD."""
    if not repo_path:
        return '', ''
    try:
        sha = subprocess.check_output(
            ['git', '-C', repo_path, 'rev-parse', 'HEAD'],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        msg = subprocess.check_output(
            ['git', '-C', repo_path, 'log', '-1', '--format=%s'],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return sha, msg
    except Exception:
        return '', ''


def resolve_default_branch(repo_path, override=None):
    """Resolve the default/target branch of the repo."""
    if override:
        return override
    if not repo_path:
        return 'main'
    try:
        ref = subprocess.check_output(
            ['git', '-C', repo_path, 'symbolic-ref', 'refs/remotes/origin/HEAD'],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return ref.split('/')[-1]
    except Exception:
        pass
    try:
        branches_raw = subprocess.check_output(
            ['git', '-C', repo_path, 'branch', '-r'],
            stderr=subprocess.DEVNULL,
        ).decode()
        for candidate in ('main', 'master', 'develop', 'trunk'):
            if f'origin/{candidate}' in branches_raw:
                return candidate
    except Exception:
        pass
    return 'main'


def load_project_gitflow(repo_id):
    """Load gitflow config for a project from the ES flume-projects index."""
    default = {
        'autoPrOnApprove': True,
        'defaultBranch': None,
        # Integration: feature branches merge here; clone worktrees from this ref.
        'integrationBranch': 'develop',
        # Production: final release PR targets this branch (human merge).
        'releaseBranch': 'main',
        'autoMergeIntegrationPr': True,
        # Create integration branch on remote from default tip if missing (gitflow).
        'ensureIntegrationBranch': True,
    }
    if not repo_id:
        return default
    try:
        res = _es_projects_request_worker(f"/flume-projects/_doc/{repo_id}")
        src = res.get('_source') or {}
        gf = dict(default)
        gf.update(src.get('gitflow') or {})
        return gf
    except Exception:
        return default


def resolve_pr_base_branch(repo_id: str) -> str:
    """Base branch for feature/story PRs (typically develop)."""
    gf = load_project_gitflow(repo_id)
    if gf.get('integrationBranch'):
        return str(gf['integrationBranch'])
    if gf.get('defaultBranch'):
        return str(gf['defaultBranch'])
    rp = load_project_repo_path(repo_id)
    return resolve_default_branch(str(rp or ''), override=None)


def resolve_release_branch(repo_id: str) -> str:
    """Production branch for the final develop → main promotion PR."""
    gf = load_project_gitflow(repo_id)
    if gf.get('releaseBranch'):
        return str(gf['releaseBranch'])
    if gf.get('defaultBranch'):
        return str(gf['defaultBranch'])
    return 'main'


def _should_delete_remote_branch_after_merge() -> bool:
    """After merging into the integration branch, remove the feature branch on the remote."""
    v = os.environ.get('FLUME_DELETE_REMOTE_BRANCH_AFTER_MERGE', '1').strip().lower()
    return v not in ('0', 'false', 'no', 'off')


def _delete_remote_branch_after_merge(
    task: dict,
    *,
    client: object | None = None,
    repo_path: Path | str | None = None,
) -> bool:
    """
    Delete the task's remote branch after its PR was merged into develop.
    Returns True if the ref was removed or already absent.
    """
    branch = (task.get('branch') or '').strip()
    if not branch:
        return False
    if client is not None and type(client).__name__ == 'GitHubClient':
        try:
            from utils.git_host_client import GitHostError, GitHostNotFoundError  # noqa

            client.delete_remote_branch(branch)
            log(f"delete_remote_branch: removed {branch!r} via GitHub API")
            return True
        except GitHostNotFoundError:
            log(f"delete_remote_branch: branch {branch!r} already absent (ok)")
            return True
        except GitHostError as e:
            log(f"delete_remote_branch: GitHub API failed for {branch!r}: {e}")
            return False
        except Exception as e:
            log(f"delete_remote_branch: unexpected error for {branch!r}: {e}")
            return False
    if repo_path:
        try:
            vr = subprocess.run(
                ['gh', 'repo', 'view', '--json', 'nameWithOwner', '-q', '.nameWithOwner'],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if vr.returncode != 0 or not (vr.stdout or '').strip():
                log(f"delete_remote_branch: gh repo view failed: {(vr.stderr or '')[:200]}")
                return False
            nwo = vr.stdout.strip()
            enc = urllib.parse.quote(branch, safe='')
            dr = subprocess.run(
                ['gh', 'api', '-X', 'DELETE', f'/repos/{nwo}/git/refs/heads/{enc}'],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if dr.returncode != 0:
                err = (dr.stderr or dr.stdout or '')[:400]
                if 'not found' in err.lower() or '404' in err:
                    log(f"delete_remote_branch: branch {branch!r} already absent (ok)")
                    return True
                log(f"delete_remote_branch: gh api DELETE failed: {err}")
                return False
            log(f"delete_remote_branch: removed {branch!r} via gh api")
            return True
        except Exception as e:
            log(f"delete_remote_branch: gh exception for {branch!r}: {e}")
            return False
    log(f"delete_remote_branch: no client/repo_path for branch {branch!r}")
    return False


def create_pr_for_task(task, reviewer_model, es_id: str | None = None):
    """
    Create a pull request via the GitHostClient REST API (AP-4B: no local clone required).
    Falls back to `gh pr create` for local repos or when the API client is unavailable.
    Returns (pr_url, pr_number, error).
    """
    task_id = task.get('id', 'unknown')
    branch  = task.get('branch')
    if not branch:
        log(f"create_pr: no branch on task={task_id}, skipping")
        return None, None, 'no_branch'

    repo_id = task.get('repo')
    src     = _get_project_source(repo_id) or {}
    target_branch = resolve_pr_base_branch(repo_id)

    title = task.get('title') or f"Task {task_id}"
    ac    = task.get('acceptance_criteria') or []
    ac_lines   = '\n'.join(f'- {c}' for c in ac) if ac else '_None recorded_'
    commit_sha  = task.get('commit_sha') or ''
    sha_line    = f'\n\n**Commit:** `{commit_sha}`' if commit_sha else ''

    clone_status = src.get('clone_status') or src.get('cloneStatus') or ''

    # ── Path A: remote repo → use GitHostClient REST API ────────────────────
    # This path requires no local clone and works from any pod.
    if clone_status not in ('local',):
        try:
            from utils.git_host_client import get_git_client, GitHostError  # noqa
            client = get_git_client(src)

            # Idempotency for story-scoped branches: reuse an existing open PR for this head.
            if hasattr(client, 'owner') and hasattr(client, '_get'):
                try:
                    pulls = client._get(
                        'pulls',
                        {
                            'state': 'open',
                            'head': f'{client.owner}:{branch}',
                            'base': target_branch,
                            'per_page': '5',
                        },
                    )
                    if isinstance(pulls, list) and pulls:
                        p0 = pulls[0]
                        pr_url = p0.get('html_url', '')
                        pr_number = p0.get('number')
                        log(f"create_pr: existing open PR for head={branch} -> {pr_url}")
                        _maybe_auto_merge_integration_pr(
                            task, pr_number, client=client, es_id=es_id,
                        )
                        return pr_url, pr_number, None
                except Exception as ex:
                    log(f"create_pr: list open PRs failed (non-fatal) task={task_id}: {ex}")

            body = (
                f"## {title}\n\n"
                f"**Task ID:** `{task_id}`\n"
                f"**Repo:** `{repo_id}`\n"
                f"**Branch:** `{branch}` → `{target_branch}`\n"
                f"**Model:** `{reviewer_model}`\n"
                f"{sha_line}\n\n"
                f"### Acceptance Criteria\n{ac_lines}\n\n"
                f"_Auto-generated by Flume agent workflow._"
            )
            result = client.create_pull_request(
                title=title, body=body, head=branch, base=target_branch,
            )
            pr_url    = result.get('pr_url', '')
            pr_number = result.get('pr_number')
            log(f"create_pr: REST API PR created task={task_id} -> {pr_url}")
            _maybe_auto_merge_integration_pr(task, pr_number, client=client, es_id=es_id)
            return pr_url, pr_number, None
        except Exception as e:
            log(f"create_pr: REST API attempt failed task={task_id}: {e}; falling back to gh CLI")

    # ── Path B: local repo or API fallback → use gh CLI ─────────────────────
    repo_path = load_project_repo_path(repo_id)
    if not repo_path or not (repo_path / '.git').exists():
        log(f"create_pr: no local repo path available for task={task_id}")
        return None, None, 'no_repo'

    # Idempotency: reuse existing open PR
    try:
        list_res = subprocess.run(
            ['gh', 'pr', 'list', '--head', branch, '--base', target_branch,
             '--state', 'open', '--json', 'url,number', '--limit', '1'],
            capture_output=True, text=True, timeout=20,
        )
        if list_res.returncode == 0 and list_res.stdout.strip():
            arr = json.loads(list_res.stdout)
            if arr:
                pr_url = arr[0].get('url')
                pr_number = arr[0].get('number')
                _maybe_auto_merge_integration_pr(
                    task, pr_number, repo_path=repo_path, es_id=es_id,
                )
                return pr_url, pr_number, None
    except Exception:
        pass

    body = (
        f"## {title}\n\n"
        f"**Task ID:** `{task_id}`\n"
        f"**Branch:** `{branch}` → `{target_branch}`\n"
        f"**Model:** `{reviewer_model}`\n"
        f"{sha_line}\n\n"
        f"### Acceptance Criteria\n{ac_lines}\n\n"
        f"_Auto-generated by Flume agent workflow._"
    )

    gh_check = subprocess.run(['which', 'gh'], capture_output=True, text=True)
    if not gh_check.stdout.strip():
        log(f"create_pr: gh CLI not available for task={task_id}")
        return None, None, 'gh_not_found'

    try:
        result = subprocess.run(
            ['gh', 'pr', 'create', '--base', target_branch, '--head', branch,
             '--title', title, '--body', body],
            capture_output=True, text=True, timeout=60,
            cwd=str(repo_path),
        )
    except subprocess.TimeoutExpired:
        log(f"create_pr: gh pr create timed out for task={task_id}")
        return None, None, 'timeout'

    if result.returncode != 0:
        err = (result.stderr or result.stdout or '').strip()[:300]
        log(f"create_pr: gh pr create failed for task={task_id}: {err}")
        return None, None, err

    pr_url    = result.stdout.strip()
    pr_number = None
    url_parts = pr_url.rstrip('/').split('/')
    if url_parts and url_parts[-1].isdigit():
        pr_number = int(url_parts[-1])

    log(f"create_pr: PR created task={task_id} -> {pr_url}")
    _maybe_auto_merge_integration_pr(task, pr_number, repo_path=repo_path, es_id=es_id)
    return pr_url, pr_number, None


_MERGE_CONFLICT_KEYWORDS = (
    'conflict',
    'merge commit cannot be cleanly created',
    'not mergeable',
    'pull request is not mergeable',
)


def _looks_like_merge_conflict(err_text: str) -> bool:
    e = (err_text or '').lower()
    return any(k in e for k in _MERGE_CONFLICT_KEYWORDS)


def _record_merge_conflict(
    task: dict,
    pr_number: int,
    *,
    client: object = None,
    repo_path: Path | str | None = None,
    es_id: str | None = None,
    err_text: str = '',
) -> None:
    """
    Annotate a task that failed to auto-merge due to a conflict and transition
    it back to `blocked` (needs_human=false) so the auto-unblocker can nudge
    an implementer through the rebase/resolve/force-push loop.
    """
    task_id = task.get('id')
    head_branch = task.get('branch') or ''
    repo_id = task.get('repo') or ''
    gf = load_project_gitflow(repo_id) or {}
    base_branch = gf.get('integrationBranch') or 'develop'

    # Best-effort: pull PR files via REST so the agent knows which files conflict.
    files_preview: list[str] = []
    pr_url = task.get('pr_url') or ''
    try:
        if client is not None and type(client).__name__ == 'GitHubClient':
            pr_info = {}
            try:
                pr_info = client.get_pull_request(int(pr_number)) or {}
            except Exception:
                pr_info = {}
            pr_url = pr_url or pr_info.get('html_url') or pr_url
            if pr_info.get('head', {}).get('ref'):
                head_branch = head_branch or pr_info['head']['ref']
            if pr_info.get('base', {}).get('ref'):
                base_branch = pr_info['base']['ref'] or base_branch
            try:
                pr_files = client.get_pull_request_files(int(pr_number)) or []
                files_preview = [
                    (f.get('filename') or '')
                    for f in pr_files[:15]
                    if (f.get('filename') or '')
                ]
            except Exception:
                files_preview = []
    except Exception as e:
        log(f'record_merge_conflict: pr-info lookup failed task={task_id}: {e}')

    conflict_payload = {
        'merge_conflict': True,
        'merge_conflict_pr_number': int(pr_number),
        'merge_conflict_pr_url': pr_url,
        'merge_conflict_head_branch': head_branch,
        'merge_conflict_base_branch': base_branch,
        'merge_conflict_files_preview': files_preview,
        'merge_conflict_last_error': (err_text or '')[:600],
    }
    note = (
        '[Merge conflict] Auto-merge into the integration branch failed.\n'
        f'- PR: {pr_url or "#" + str(pr_number)}\n'
        f'- Rebase {head_branch!r} onto latest origin/{base_branch!r} and resolve conflicts.\n'
        f'- Suggested commands:\n'
        f'    git fetch origin {base_branch} {head_branch}\n'
        f'    git checkout {head_branch}\n'
        f'    git rebase origin/{base_branch}\n'
        f'    # resolve conflicts in-place, then:\n'
        f'    git add -A && git rebase --continue\n'
        f'    git push --force-with-lease origin {head_branch}\n'
        f'- Conflicting files (first 15): {files_preview or "(not available — inspect PR)"}\n'
        '- After pushing, the dashboard will retry the integration merge automatically.'
    )

    try:
        append_agent_note(es_id or task_id, note)
    except Exception:
        pass

    if es_id:
        try:
            update_task_doc(es_id, {
                'status': 'blocked',
                'needs_human': False,
                'queue_state': 'queued',
                'active_worker': None,
                **conflict_payload,
            })
            log(
                f"auto-merge conflict recorded task={task_id} pr={pr_number} "
                f"files={len(files_preview)}"
            )
        except Exception as e:
            log(f"record_merge_conflict: update_task_doc failed task={task_id}: {e}")


_INTEGRATION_LOCK_INDEX = 'flume-integration-locks'
_INTEGRATION_LOCK_TTL_SECONDS = 180


def _acquire_integration_merge_lock(repo_id: str, task_id: str, pr_number: int) -> bool:
    """Acquire per-repo integration-merge lock via ES op_type=create (atomic)."""
    if not repo_id:
        return True
    now = datetime.now(timezone.utc).isoformat()
    body = {
        'repo': repo_id,
        'task_id': task_id,
        'pr_number': pr_number,
        'acquired_at': now,
    }
    try:
        _es_projects_request_worker(
            f'/{_INTEGRATION_LOCK_INDEX}/_create/{repo_id}?refresh=true',
            body=body,
            method='PUT',
        )
        return True
    except urllib.error.HTTPError as e:
        if e.code != 409:
            log(f"integration lock acquire unexpected error repo={repo_id}: {e}")
            return False
    except Exception as e:
        log(f"integration lock acquire failed repo={repo_id}: {e}")
        return False
    # Stale-lock reclaim: if the existing lock is older than TTL, force-release and retry once.
    try:
        existing = _es_projects_request_worker(f'/{_INTEGRATION_LOCK_INDEX}/_doc/{repo_id}', method='GET')
        src = (existing or {}).get('_source') or {}
        acquired_at = src.get('acquired_at')
        if acquired_at:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(acquired_at.replace('Z', '+00:00'))).total_seconds()
            if age > _INTEGRATION_LOCK_TTL_SECONDS:
                log(f"integration lock: releasing stale lock repo={repo_id} age={age:.0f}s")
                _release_integration_merge_lock(repo_id)
                try:
                    _es_projects_request_worker(
                        f'/{_INTEGRATION_LOCK_INDEX}/_create/{repo_id}?refresh=true',
                        body=body,
                        method='PUT',
                    )
                    return True
                except Exception as ee:
                    log(f"integration lock re-acquire after stale release failed repo={repo_id}: {ee}")
                    return False
    except Exception as e:
        log(f"integration lock stale-check failed repo={repo_id}: {e}")
    return False


def _release_integration_merge_lock(repo_id: str) -> None:
    if not repo_id:
        return
    try:
        _es_projects_request_worker(
            f'/{_INTEGRATION_LOCK_INDEX}/_doc/{repo_id}?refresh=true',
            method='DELETE',
        )
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log(f"integration lock release error repo={repo_id}: {e}")
    except Exception as e:
        log(f"integration lock release failed repo={repo_id}: {e}")


def _serialize_integration_merge_enabled(repo_id: str) -> bool:
    try:
        from utils.concurrency_config import serialize_integration_merge  # noqa: PLC0415
    except Exception:
        return True
    src = _get_project_source(repo_id) if repo_id else None
    return bool(serialize_integration_merge(src))


def _maybe_auto_merge_integration_pr(
    task: dict,
    pr_number: int | None,
    *,
    client: object = None,
    repo_path: Path | str | None = None,
    es_id: str | None = None,
) -> None:
    """
    Merge feature PR into the integration branch (develop) when enabled, then
    optionally delete the remote task branch so develop is the single line of integration.
    Never runs for release-promotion tasks.

    Serializes merges per-repo via an ES CAS lock so two feature PRs don't race
    into develop at the same instant -- the primary cause of avoidable merge
    conflicts under parallel work.
    """
    if not pr_number or task.get('release_promotion_task'):
        return
    repo_id = task.get('repo')
    gf = load_project_gitflow(repo_id)
    if not gf.get('autoMergeIntegrationPr', True):
        return

    lock_held = False
    if _serialize_integration_merge_enabled(repo_id):
        lock_held = _acquire_integration_merge_lock(repo_id, str(task.get('id') or ''), int(pr_number))
        if not lock_held:
            log(
                f"auto-merge deferred task={task.get('id')} pr={pr_number} repo={repo_id}: "
                f"another merge holds the integration lock; retry on next cycle"
            )
            if es_id:
                try:
                    update_task_doc(es_id, {'pr_status': 'awaiting_integration_merge'})
                except Exception:
                    pass
            return
    try:
        _do_auto_merge_integration_pr(task, pr_number, client=client, repo_path=repo_path, es_id=es_id, gf=gf)
    finally:
        if lock_held:
            _release_integration_merge_lock(repo_id)


def _do_auto_merge_integration_pr(
    task: dict,
    pr_number: int,
    *,
    client: object,
    repo_path: Path | str | None,
    es_id: str | None,
    gf: dict,
) -> None:
    merged_ok = False
    if client is not None and type(client).__name__ == 'GitHubClient':
        try:
            client.merge_pull_request(int(pr_number))
            merged_ok = True
            log(f"auto-merge: merged PR #{pr_number} for task={task.get('id')}")
        except Exception as e:
            err = str(e).lower()
            if 'already merged' in err or 'pull request is already merged' in err:
                merged_ok = True
                log(f"auto-merge: PR #{pr_number} already merged for task={task.get('id')}")
            elif _looks_like_merge_conflict(err):
                log(f"auto-merge conflict task={task.get('id')} pr={pr_number}: {e}")
                _record_merge_conflict(
                    task, int(pr_number),
                    client=client, repo_path=repo_path, es_id=es_id,
                    err_text=str(e),
                )
                return
            else:
                log(f"auto-merge GitHub API failed (non-fatal) task={task.get('id')}: {e}")
        if merged_ok:
            deleted = False
            if _should_delete_remote_branch_after_merge():
                deleted = _delete_remote_branch_after_merge(task, client=client)
            if es_id:
                try:
                    doc = {'pr_status': 'merged'}
                    if deleted:
                        doc['remote_branch_deleted'] = True
                    update_task_doc(es_id, doc)
                except Exception as ex:
                    log(f"auto-merge: update_task_doc after merge failed task={task.get('id')}: {ex}")
        return
    if repo_path:
        try:
            mr = subprocess.run(
                ['gh', 'pr', 'merge', str(pr_number), '--merge'],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if mr.returncode != 0:
                raw = (mr.stderr or mr.stdout or '')
                err = raw.lower()
                if 'already merged' in err or 'nothing to merge' in err:
                    merged_ok = True
                    log(f"auto-merge: PR #{pr_number} already merged (gh) for task={task.get('id')}")
                elif _looks_like_merge_conflict(err):
                    log(f"auto-merge conflict (gh) task={task.get('id')} pr={pr_number}: {raw[:300]}")
                    _record_merge_conflict(
                        task, int(pr_number),
                        client=client, repo_path=repo_path, es_id=es_id,
                        err_text=raw,
                    )
                    return
                else:
                    log(f"auto-merge gh failed task={task.get('id')}: {raw[:400]}")
            else:
                merged_ok = True
                log(f"auto-merge: gh merged PR #{pr_number} for task={task.get('id')}")
        except Exception as e:
            log(f"auto-merge gh exception task={task.get('id')}: {e}")
        if merged_ok:
            deleted = False
            if _should_delete_remote_branch_after_merge():
                deleted = _delete_remote_branch_after_merge(task, repo_path=repo_path)
            if es_id:
                try:
                    doc = {'pr_status': 'merged'}
                    if deleted:
                        doc['remote_branch_deleted'] = True
                    update_task_doc(es_id, doc)
                except Exception as ex:
                    log(f"auto-merge: update_task_doc after gh merge failed task={task.get('id')}: {ex}")


def create_release_promotion_pr(task: dict) -> tuple[str | None, int | None, str | None]:
    """
    Open develop → main (or configured integration → release) for human merge.
    No local clone required (GitHub REST).
    """
    repo_id = task.get('repo')
    src = _get_project_source(repo_id) or {}
    clone_status = src.get('clone_status') or src.get('cloneStatus') or ''
    if clone_status in ('local',):
        return None, None, 'release_pr_requires_remote_api'

    gf = load_project_gitflow(repo_id)
    ib = (gf.get('integrationBranch') or 'develop').strip()
    rb = resolve_release_branch(repo_id)

    try:
        from utils.git_host_client import get_git_client  # noqa
        client = get_git_client(src)
    except Exception as e:
        return None, None, str(e)

    title = f"Release: merge {ib} → {rb}"
    body = (
        f"## Release promotion\n\n"
        f"**Repo:** `{repo_id}`\n"
        f"**Integration:** `{ib}` → **Release:** `{rb}`\n\n"
        f"Flume opened this PR automatically when all epics in the plan completed. "
        f"**Merge manually** after review — Flume does not auto-merge this PR.\n"
    )

    if hasattr(client, 'owner') and hasattr(client, '_get'):
        try:
            pulls = client._get(
                'pulls',
                {
                    'state': 'open',
                    'head': f'{client.owner}:{ib}',
                    'base': rb,
                    'per_page': '5',
                },
            )
            if isinstance(pulls, list) and pulls:
                p0 = pulls[0]
                return p0.get('html_url'), p0.get('number'), None
        except Exception as ex:
            log(f"create_release_promotion_pr: list pulls failed: {ex}")

    try:
        result = client.create_pull_request(title=title, body=body, head=ib, base=rb)
        return result.get('pr_url'), result.get('pr_number'), None
    except Exception as e:
        return None, None, str(e)


def _handle_release_promotion_task(task: dict, es_id: str) -> bool:
    """No clone: open the final integration→release PR via API."""
    task_id = task.get('id', '')
    append_agent_note(es_id, 'Opening release promotion PR (integration → release branch)…')
    try:
        pr_url, pr_number, err = create_release_promotion_pr(task)
        if err:
            append_agent_note(es_id, f'Release PR failed: {err}')
            update_task_doc(
                es_id,
                {
                    'status': 'blocked',
                    'needs_human': True,
                    **_implementer_clear_claim_fields(),
                },
            )
            log(f"release_promotion_task={task_id} failed: {err}")
            return True
        update_task_doc(
            es_id,
            {
                'status': 'done',
                'pr_url': pr_url,
                'pr_number': pr_number,
                'pr_status': 'open',
                'target_branch': resolve_release_branch(task.get('repo') or ''),
                'needs_human': True,
                'owner': 'implementer',
                'assigned_agent_role': 'implementer',
                **_implementer_clear_claim_fields(),
            },
        )
        append_agent_note(
            es_id,
            'Release PR is open. Merge it manually when you are ready (Flume never auto-merges this PR).',
        )
        log(f"release_promotion_task={task_id} opened PR {pr_url}")
    except Exception as e:
        log(f"release_promotion_task={task_id} exception: {e}")
        append_agent_note(es_id, f'Release PR error: {e}')
        update_task_doc(
            es_id,
            {'status': 'blocked', 'needs_human': True, **_implementer_clear_claim_fields()},
        )
    return True


def _bug_recursion_depth(task_id: str) -> int:
    """Count how many times a task has recursed through the bug-fix pipeline.

    Bug tasks are named `bug-<parent_id>-<idx>`, so a chain like
    `task-abc → bug-task-abc-1 → bug-bug-task-abc-1-1` has depth 0, 1, 2
    respectively. This is the cheapest, most reliable way to detect runaway
    bug-fix loops without walking the parent chain through Elasticsearch.
    """
    depth = 0
    tid = task_id or ""
    while tid.startswith("bug-"):
        tid = tid[4:]
        depth += 1
    return depth


def create_bug_task(parent_task, bug, idx):
    """
    Idempotently create (or re-open) the follow-up bug task for *parent_task*.

    Historically this function blindly indexed a new document every time the
    tester failed, which caused Elasticsearch to accumulate dozens of records
    with identical logical ids (`bug-<parent>-<idx>`) — each spawning its own
    random branch. We now:

      1. Use `PUT _create/<bug_id>` so the ES `_id` is the logical bug id,
         making duplicates physically impossible.
      2. If a doc already exists for this bug id, reopen it instead of
         creating a new one — preserving its branch so retries stack on top
         of prior implementer work.
    """
    parent_id = parent_task.get('id', 'task')
    bug_id = f"bug-{parent_id}-{idx}"
    title = bug.get('title', f"Bug follow-up for {parent_task.get('title', 'task')}")
    objective = bug.get('objective', 'Fix defect identified during tester validation.')
    priority = 'high' if bug.get('severity') == 'high' else 'normal'

    existing_es_id, existing_src = fetch_task_doc(bug_id)
    if existing_es_id and existing_src:
        prev_status = str(existing_src.get('status') or '').lower()
        terminal = prev_status in ('done', 'archived', 'cancelled', 'blocked')
        patch: dict = {
            'objective': objective,
            'title': title,
            'priority': priority,
            'updated_at': now_iso(),
            'last_update': now_iso(),
        }
        if terminal:
            patch.update({
                'status': 'ready',
                'queue_state': 'queued',
                'active_worker': None,
                'owner': 'implementer',
                'assigned_agent_role': 'implementer',
                'needs_human': False,
            })
        try:
            update_task_doc(existing_es_id, patch)
            if terminal:
                append_agent_note(
                    existing_es_id,
                    f'Re-opened by tester (was {prev_status}). Reusing existing branch '
                    f'{existing_src.get("branch") or "(none)"} to avoid orphan branches.',
                )
            log(
                f"create_bug_task: reused existing {bug_id} "
                f"(prev_status={prev_status}, branch={existing_src.get('branch')})"
            )
        except Exception as e:
            log(f"create_bug_task: reopen failed for {bug_id}: {e}")
        return

    doc = {
        'id': bug_id,
        'title': title,
        'objective': objective,
        'repo': parent_task.get('repo'),
        'worktree': parent_task.get('worktree'),
        'item_type': 'bug',
        # Link back to the originating task so the dashboard's parent-revival
        # sweep can re-queue the parent once this bug is closed.
        'parent_id': parent_id,
        'origin_task_id': parent_id,
        'branch': parent_task.get('branch'),
        'owner': 'implementer',
        'assigned_agent_role': 'implementer',
        'status': 'ready',
        'priority': priority,
        'depends_on': [],
        'acceptance_criteria': [],
        'artifacts': [],
        'needs_human': False,
        'risk': 'medium',
        'created_at': now_iso(),
        'updated_at': now_iso(),
        'last_update': now_iso(),
    }
    try:
        es_request(
            f'/{TASK_INDEX}/_create/{bug_id}?refresh=wait_for',
            doc,
            method='PUT',
        )
    except urllib.error.HTTPError as e:
        if getattr(e, 'code', None) == 409:
            log(f"create_bug_task: race — {bug_id} created concurrently; skipping duplicate")
            return
        log(f"create_bug_task: PUT _create/{bug_id} failed: {e}; falling back to POST _doc")
        try:
            write_doc(TASK_INDEX, doc)
        except Exception as ee:
            log(f"create_bug_task: fallback POST for {bug_id} failed: {ee}")
    except Exception as e:
        log(f"create_bug_task: unexpected error creating {bug_id}: {e}")


def compute_ready_for_repo(repo):
    """
    Walk the full task hierarchy for a repo and:
    1. Promote 'planned' leaf tasks to 'ready' when all their deps are 'done'.
    2. Mark parent epics/features/stories 'done' when every child item is 'done'.
    3. Return the count of status changes made.
    """
    if not repo:
        return 0
    # Some deployments store `repo` only in _source (not indexed), so querying
    # on repo may return 0 hits. Pull the task set and filter in-memory instead.
    res = es_request(
        f'/{TASK_INDEX}/_search',
        {'size': 500, 'query': {'bool': {'must_not': [{'term': {'status': 'archived'}}]}}},
        method='POST',
    )
    hits = res.get('hits', {}).get('hits', [])
    by_id = {}
    for h in hits:
        src = h.get('_source', {})
        if src.get('repo') != repo:
            continue
        src['_es_id'] = h.get('_id')
        by_id[src.get('id')] = src

    # Build reverse map: parent_id -> [child ids] using explicit parent_id field.
    # Fall back to depends_on for legacy items that pre-date the parent_id field,
    # but only treat a dep as a parent when its item_type is one level above.
    type_parent = {'feature': 'epic', 'story': 'feature', 'task': 'story'}
    children_of: dict = {}
    for item_id, src in by_id.items():
        explicit_parent = src.get('parent_id')
        if explicit_parent and explicit_parent in by_id:
            children_of.setdefault(explicit_parent, []).append(item_id)
        else:
            # Legacy: infer parent from depends_on by matching expected parent type
            expected_parent_type = type_parent.get(src.get('item_type', ''))
            for dep in (src.get('depends_on') or []):
                dep_src = by_id.get(dep)
                if dep_src and dep_src.get('item_type') == expected_parent_type:
                    children_of.setdefault(dep, []).append(item_id)
                    break

    changed = 0

    # --- WIP gate (repo/story parallelism) -------------------------------------
    # Compute current "active" (ready+running+review) counts so we don't promote
    # more leaf work than the project's concurrency settings allow.
    try:
        from utils.concurrency_config import (  # noqa: PLC0415
            max_ready_for_repo,
            story_parallelism,
        )
        _proj_src = _get_project_source(repo) or {}
        _repo_cap = int(max_ready_for_repo(_proj_src) or 0)
        _story_cap = int(story_parallelism(_proj_src) or 0)
    except Exception:
        _repo_cap = 0
        _story_cap = 0

    _rollup_types = {'epic', 'feature', 'story'}
    # `blocked` is included because a task that blocked mid-pipeline (e.g. on a
    # merge conflict awaiting pr_reconcile) still owns an unmerged branch.
    # Promoting another task while that branch is in flight is exactly what
    # causes the multi-branch merge-conflict cascade we are trying to avoid.
    _active_states = {'ready', 'running', 'review', 'blocked'}

    def _counts_as_wip(item: dict) -> bool:
        if item.get('status') not in _active_states:
            return False
        it = (item.get('item_type') or 'task').lower()
        if it in _rollup_types:
            return False
        if (item.get('owner') or '').lower() == 'pm':
            return False
        if (item.get('assigned_agent_role') or '').lower() == 'pm':
            return False
        # A blocked task only contributes to WIP if it still has an unmerged
        # branch (merge_conflict flag, or a commit_sha without pr_merged=True).
        # Blocked bug tasks with no branch shouldn't hold up the pipeline.
        if item.get('status') == 'blocked':
            if item.get('pr_merged') is True:
                return False
            has_branch = bool(item.get('branch')) or bool(item.get('commit_sha'))
            if not has_branch:
                return False
        return True

    # Saturation is measured in *distinct branches in flight*, not raw WIP
    # count. That way promoting another task onto an already-in-flight branch
    # (e.g. a follow-up task under the same story scope) is fine; only NEW
    # branches are gated.
    in_flight_branches: set = set()
    active_story_count: dict = {}
    for s in by_id.values():
        if not _counts_as_wip(s):
            continue
        br = (s.get('branch') or '').strip()
        if br:
            in_flight_branches.add(br)
        pid = s.get('parent_id') or ''
        if pid:
            active_story_count[pid] = active_story_count.get(pid, 0) + 1
    active_repo_branch_count = len(in_flight_branches)

    # Pass 1: promote 'planned' items to 'ready' when all deps are done
    for item_id, src in by_id.items():
        if not item_id or src.get('status') != 'planned':
            continue
        item_type = src.get('item_type', 'task')
        deps = src.get('depends_on') or []

        if deps:
            if not all(by_id.get(dep, {}).get('status') == 'done' for dep in deps):
                continue
        else:
            if item_type != 'task':
                continue

        is_leaf = (item_type or 'task').lower() not in _rollup_types
        if is_leaf and _repo_cap:
            # Pre-compute the branch this task would land on once promoted.
            # If the task or any of its done-deps already has a branch, and
            # that branch is already in flight, continuing on it is free —
            # no new branch will be cut, so no saturation concern.
            prospective_branch = (src.get('branch') or '').strip()
            if not prospective_branch:
                for dep in deps:
                    dsrc = by_id.get(dep)
                    if dsrc and dsrc.get('branch'):
                        prospective_branch = (dsrc.get('branch') or '').strip()
                        if prospective_branch:
                            break
            would_open_new_branch = (
                not prospective_branch
                or prospective_branch not in in_flight_branches
            )
            if would_open_new_branch and active_repo_branch_count >= _repo_cap:
                continue
        if is_leaf and _story_cap:
            pid = src.get('parent_id') or ''
            if pid and active_story_count.get(pid, 0) >= _story_cap:
                continue
        
        # Infer role/requirements for task items if missing
        patch = {'status': 'ready'}
        if src.get('item_type') == 'task':
            title = (src.get('title') or '').lower()
            current_role = src.get('assigned_agent_role')
            if not current_role or current_role == 'pm':
                if 'review' in title or 'approve' in title:
                    patch['assigned_agent_role'] = 'reviewer'
                    patch['owner'] = 'reviewer'
                elif 'test' in title or 'validate' in title or 'qa' in title:
                    patch['assigned_agent_role'] = 'tester'
                    patch['owner'] = 'tester'
                else:
                    patch['assigned_agent_role'] = 'implementer'
                    patch['owner'] = 'implementer'
            if src.get('requires_code') is None and any(k in title for k in ['update', 'modify', 'implement', 'change', 'edit', 'replace', 'add ', 'remove ', 'create']):
                patch['requires_code'] = True
            # If this task depends on a completed task with a commit, inherit commit metadata
            if deps:
                ctx_notes = []
                for dep_id in deps:
                    dep = by_id.get(dep_id)
                    if not dep:
                        continue
                    if not src.get('commit_sha') and dep.get('commit_sha'):
                        patch['commit_sha'] = dep.get('commit_sha')
                        patch['commit_message'] = dep.get('commit_message')
                        patch['branch'] = dep.get('branch')
                        patch['worktree'] = dep.get('worktree')
                    if dep.get('context_summary'):
                        ctx_notes.append(f"Context from {dep_id}:\n{dep['context_summary']}")
                if ctx_notes:
                    patch['dependency_context'] = "\n\n".join(ctx_notes)
        update_task_doc(src['_es_id'], patch)
        src.update(patch)  # update local view
        changed += 1
        if is_leaf:
            promoted_branch = (patch.get('branch') or src.get('branch') or '').strip()
            if promoted_branch and promoted_branch not in in_flight_branches:
                in_flight_branches.add(promoted_branch)
                active_repo_branch_count = len(in_flight_branches)
            elif not promoted_branch:
                # Fresh branch will be cut by ensure_task_branch; reserve a slot.
                active_repo_branch_count += 1
            pid = src.get('parent_id') or ''
            if pid:
                active_story_count[pid] = active_story_count.get(pid, 0) + 1
        log(f"compute_ready: promoted {item_id} to ready")

    # Pass 2: mark parent items 'done' when all their children are 'done'
    # Process from leaf parents up (stories first, then features, then epics)
    # so a story becoming 'done' can trigger a feature becoming 'done' in the same pass.
    # A child is considered "terminal" if its status is 'done' or 'archived',
    # OR if its queue_state is 'skipped' (dedup gate marked it as redundant).
    def _is_terminal(item):
        if not item:
            return False
        if item.get('status') in ('done', 'archived'):
            return True
        if item.get('queue_state') == 'skipped':
            return True
        return False

    type_order = {'task': 0, 'story': 1, 'feature': 2, 'epic': 3}
    parents = sorted(
        [item_id for item_id in children_of if item_id in by_id],
        key=lambda x: type_order.get(by_id[x].get('item_type', 'epic'), 3),
    )
    for parent_id in parents:
        src = by_id.get(parent_id)
        if not src or src.get('status') in ('done', 'archived'):
            continue
        child_ids = children_of.get(parent_id, [])
        if not child_ids:
            continue
        all_terminal = all(_is_terminal(by_id.get(cid)) for cid in child_ids)
        if all_terminal:
            update_task_doc(src['_es_id'], {'status': 'done', 'active_worker': None, 'queue_state': 'queued'})
            src['status'] = 'done'  # update local view for subsequent passes
            changed += 1
            log(f"compute_ready: marked parent {parent_id} ({src.get('item_type')}) done — all children terminal")

    extra = _maybe_enqueue_release_promotion_task(repo, by_id)
    return changed + extra


def _maybe_enqueue_release_promotion_task(repo: str, by_id: dict) -> int:
    """
    When every epic in the repo is done, enqueue a single task to open
    integration → release PR (human merge only).
    """
    epic_ids = [eid for eid, src in by_id.items() if src.get('item_type') == 'epic']
    if not epic_ids:
        return 0
    if not all(by_id[e].get('status') in ('done', 'archived') for e in epic_ids):
        return 0
    rid = f"release-d2m-{repo}"
    if rid in by_id:
        return 0
    doc = {
        'id': rid,
        'repo': repo,
        'parent_id': None,
        'title': 'Release: open PR to merge develop → main (human merge)',
        'objective': (
            'All epics for this plan are complete. Open one pull request from the integration branch '
            '(develop) to the release branch (main). Flume does not auto-merge this PR — merge manually after review.'
        ),
        'item_type': 'task',
        'status': 'ready',
        'owner': 'implementer',
        'assigned_agent_role': 'implementer',
        'release_promotion_task': True,
        'requires_code': False,
        'depends_on': [],
        'acceptance_criteria': [
            'Pull request exists from integration branch to release branch',
            'PR is left open for human review and merge',
        ],
        'artifacts': [],
        'needs_human': True,
        'risk': 'low',
        'created_at': now_iso(),
        'updated_at': now_iso(),
        'last_update': now_iso(),
    }
    try:
        write_doc(TASK_INDEX, doc)
        log(f"compute_ready: enqueued release promotion task {rid} for repo={repo}")
        return 1
    except Exception as e:
        log(f"compute_ready: failed to enqueue release task: {e}")
        return 0





def auto_commit_and_push(repo_path: str, branch: str, commit_message: str, task_id: str) -> str:
    """Stage all changes, commit, push with embedded auth, and return the new SHA. Returns '' on failure."""
    _src_root = Path(__file__).resolve().parent.parent
    _utils_path = str(_src_root)
    if _utils_path not in sys.path:
        sys.path.insert(0, _utils_path)
    from utils.git_credentials import detect_repo_type, embed_credentials, strip_credentials  # noqa

    try:
        status = subprocess.run(
            ['git', '-C', repo_path, 'status', '--porcelain'],
            capture_output=True, text=True,
        )
        if not status.stdout.strip():
            log(f"auto_commit: no changes to commit for task={task_id}")
            return ''

        # Ensure git identity is set (required in containerised/CI environments)
        email = subprocess.run(['git', '-C', repo_path, 'config', 'user.email'], capture_output=True, text=True)
        if email.returncode != 0 or not (email.stdout or '').strip():
            subprocess.run(['git', '-C', repo_path, 'config', 'user.email', 'ai-bot@flume.local'], check=True, capture_output=True)
        name = subprocess.run(['git', '-C', repo_path, 'config', 'user.name'], capture_output=True, text=True)
        if name.returncode != 0 or not (name.stdout or '').strip():
            subprocess.run(['git', '-C', repo_path, 'config', 'user.name', 'Flume AI Bot'], check=True, capture_output=True)

        subprocess.run(['git', '-C', repo_path, 'add', '-A'], check=True, capture_output=True)
        subprocess.run(
            ['git', '-C', repo_path, 'commit', '-m', commit_message or f'Implement task {task_id}'],
            check=True, capture_output=True,
        )

        # --- Credential-embedded push (token-safe) ---
        # Read the stored remote URL; strip any previously embedded credentials so we
        # get the canonical clean URL, then embed a fresh PAT for this push only.
        # We NEVER write the auth URL back to the remote config — instead we pass it
        # directly to `git push <url>` so the token only lives in process argv (ephemeral)
        # and never in .git/config.
        origin_url_res = subprocess.run(
            ['git', '-C', repo_path, 'remote', 'get-url', 'origin'],
            capture_output=True, text=True,
        )
        stored_url = (origin_url_res.stdout or '').strip()
        # Strip any leaked token from the stored URL (defensive cleanup)
        clean_url = strip_credentials(stored_url)
        if clean_url != stored_url:
            subprocess.run(
                ['git', '-C', repo_path, 'remote', 'set-url', 'origin', clean_url],
                capture_output=True,
            )
        repo_type = detect_repo_type(clean_url)
        auth_url = embed_credentials(clean_url, repo_type)

        # --- Pre-Push Rebase Protocol (Optimistic Concurrency) ---
        try:
            fetch_url = auth_url if auth_url != clean_url else 'origin'
            fetch_res = subprocess.run(
                ['git', '-C', repo_path, 'fetch', fetch_url, f'refs/heads/{branch}'],
                capture_output=True, text=True, timeout=60
            )
            # If fetch succeeds, the branch exists remotely, so we try to rebase
            if fetch_res.returncode == 0:
                rebase_res = subprocess.run(
                    ['git', '-C', repo_path, 'rebase', 'FETCH_HEAD'],
                    capture_output=True, text=True, timeout=60
                )
                if rebase_res.returncode != 0:
                    log(f"auto_commit: rebase conflict detected for task={task_id} on {branch}. Aborting.")
                    subprocess.run(['git', '-C', repo_path, 'rebase', '--abort'], capture_output=True)
                    return 'conflict'
        except Exception as e:
            log(f"auto_commit: pre-push rebase error for task={task_id}: {e}")

        push_failed = False
        push_stderr = ''
        try:
            if auth_url != clean_url:
                # Push directly to the authenticated URL without storing it in config
                subprocess.run(
                    ['git', '-C', repo_path, 'push', auth_url, f'{branch}:refs/heads/{branch}', '--set-upstream'],
                    check=True, capture_output=True, timeout=60,
                )
            else:
                # SSH or no-creds path — push normally via named remote
                subprocess.run(
                    ['git', '-C', repo_path, 'push', '-u', 'origin', branch],
                    check=True, capture_output=True, timeout=60,
                )
        except subprocess.CalledProcessError as push_err:
            push_failed = True
            raw_err = push_err.stderr.decode(errors='replace') if push_err.stderr else str(push_err)
            push_stderr = strip_credentials(raw_err[:300])

        if push_failed:
            log(f"auto_commit: push failed for task={task_id} branch={branch}: {push_stderr}")
            return ''

        sha, _ = get_latest_commit_sha(repo_path)
        log(f"auto_commit: committed and pushed task={task_id} branch={branch} sha={sha[:8] if sha else 'n/a'}")
        return sha

    except subprocess.CalledProcessError as e:
        log(f"auto_commit: git error for task={task_id}: {e.stderr.decode() if e.stderr else e}")
        return ''
    except Exception as e:
        log(f"auto_commit: unexpected error for task={task_id}: {e}")
        return ''


def _branch_has_new_commits(repo_path: str, branch: str) -> bool:
    """Return True only if branch has at least one commit that is not on origin/main (or main)."""
    for base in ('origin/develop', 'origin/main', 'origin/master', 'main', 'master'):
        try:
            out = subprocess.run(
                ['git', '-C', repo_path, 'log', f'{base}..{branch}', '--oneline', '--max-count=1'],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0:
                return bool(out.stdout.strip())
        except Exception:
            continue
    return False


def task_requires_code(task: dict) -> bool:
    """
    Heuristic: decide whether this task should result in file modifications.
    We use it to avoid marking code-changing tasks as "done" when the agent
    only did analysis/context and didn't actually edit the repo.
    """
    # Non-code overrides evaluated first — if the task is explicitly analytical,
    # documentation-oriented, or exploratory, it must NOT be re-queued just
    # because its title contains a code-sounding verb (e.g. "Research and update").
    non_code_overrides = [
        'research', 'investigate', 'analyze', 'analyse',
        'explore', 'plan ', 'design', 'discuss', 'assess', 'report', 'summarize',
        'summarise', 'audit', 'verify', 'validate', 'review ', 'test '
    ]
    if task.get('requires_code') is False:
        return False
    if task.get('release_promotion_task'):
        return False
    # Key off both title AND objective for better signal (title alone is sometimes
    # too generic when inherited from a parent story).
    full_text = f"{task.get('title', '')} {task.get('objective', '')}".lower()

    # Repo-deliverable docs (README, markdown) must produce git commits. Do not treat as
    # analysis-only: handle_implementer discards file writes when requires_code is False,
    # which left "done" tasks with nothing pushed to GitHub.
    if any(
        m in full_text
        for m in (
            'readme',
            'readme.md',
            '.md',
            'markdown',
        )
    ):
        return True

    # Non-code overrides — analytical / exploratory work without a repo artifact.
    # NOTE: Do not use the bare substring 'document' here: planner story titles often
    # start with "Document …" (meaning write that section into the repo) and would
    # incorrectly classify those as analysis-only.
    non_code_overrides = [
        'documentation', 'research', 'investigate', 'analyze', 'analyse',
        'explore', 'plan ', 'design', 'discuss', 'assess', 'report', 'summarize',
        'summarise', 'audit', 'verify', 'validate', 'review ', 'test '
    ]
    if any(t in full_text for t in non_code_overrides):
        return False

    # Code-edit / content-edit verbs. Keep this list conservative to avoid
    # flagging pure validation tasks ("verify"/"validate"/"test").
    # "document " catches Flume planner phrasing: "Document application architecture …"
    code_triggers = [
        'document ',
        'replace', 'update', 'modify', 'implement', 'write', 'add ', 'remove ',
        'create', 'change', 'set ', 'edit ', 'reorganize', 'convert', 'restructure',
        'migrate', 'rewrite', 'move ', 'rename', 'refactor', 'format', 'reformat',
        'correct', 'fix ', 'patch', 'delete', 'insert', 'append',
    ]
    return any(t in full_text for t in code_triggers)


def _implementer_clear_claim_fields() -> dict:
    """Drop manager claim markers so the task is not stuck 'running' in the UI."""
    return {'active_worker': None, 'queue_state': 'queued'}


def _implementer_max_llm_failures_cap() -> int:
    """
    Stop infinite ready→running loops when the LLM never returns a usable response.
    After this many consecutive failures the task is blocked for human attention.
    Set FLUME_IMPLEMENTER_MAX_LLM_FAILURES=0 to disable (previous behavior).
    """
    raw = os.environ.get('FLUME_IMPLEMENTER_MAX_LLM_FAILURES', '10').strip()
    try:
        n = int(raw)
    except ValueError:
        n = 3
    return max(0, n)


def _parse_implementer_llm_failure_count(task: dict) -> int:
    v = task.get('implementer_consecutive_llm_failures')
    try:
        return max(0, int(v)) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _implementer_handle_llm_failure(es_id: str, task: dict, task_id: str) -> None:
    """After LLM gave no usable output: re-queue, or block if failure cap is reached."""
    prev = _parse_implementer_llm_failure_count(task)
    next_n = prev + 1
    cap = _implementer_max_llm_failures_cap()

    if cap > 0 and next_n >= cap:
        host = task.get('execution_host', 'localhost')
        append_agent_note(
            es_id,
            f'Blocked on Node {host}: implementer hit {next_n} consecutive LLM failures (cap={cap}, '
            'FLUME_IMPLEMENTER_MAX_LLM_FAILURES). Fix LLM on the worker host '
            '(see worker_handlers.log), or set cap to 0 to retry indefinitely. '
            'Transition this task to **ready** after fixing to reset the failure counter.',
        )
        update_task_doc(
            es_id,
            {
                'status': 'blocked',
                'needs_human': True,
                'owner': 'implementer',
                'assigned_agent_role': 'implementer',
                'implementer_consecutive_llm_failures': next_n,
                **_implementer_clear_claim_fields(),
            },
        )
        log(f"implementer: task={task_id} blocked after {next_n} LLM failures (cap={cap})")
        return

    cap_hint = f' (attempt {next_n}/{cap})' if cap > 0 else f' (attempt {next_n})'
    append_agent_note(
        es_id,
        'Re-queued: LLM returned no usable response. On the host running worker_handlers.py, '
        'check LLM_PROVIDER / LLM_BASE_URL / LLM_API_KEY, that the model exists (e.g. `ollama pull <model>`), '
        'and worker_handlers.log for HTTP errors.'
        + cap_hint,
    )
    update_task_doc(
        es_id,
        {
            'status': 'ready',
            'owner': 'implementer',
            'assigned_agent_role': 'implementer',
            'needs_human': False,
            'implementer_consecutive_llm_failures': next_n,
            **_implementer_clear_claim_fields(),
        },
    )
    log(f"implementer failed to complete task={task_id} (LLM error/fallback) — re-queued for retry ({next_n}/{cap if cap > 0 else '∞'})")








def run_worker(worker):
    try:
        os.environ['FLUME_WORKER_NAME'] = worker.get('name', 'unknown')
        os.environ['FLUME_WORKER_ROLE'] = worker.get('role', 'unknown')
        task_id = worker.get('current_task_id')
        if not task_id and worker.get('role') != 'pm':
            return True
        es_id, task = fetch_task_doc(task_id) if task_id else (None, None)
        if worker['role'] == 'pm':
            from handlers.pm import handle_pm_dispatcher_worker
            return handle_pm_dispatcher_worker(task)
        if not es_id or not task:
            log(f"worker {worker['name']} could not find task={task_id}")
            return False
        if worker['role'] == 'implementer':
            from handlers.implementer import handle_implementer_worker
            return handle_implementer_worker(task, es_id)
        if worker['role'] == 'tester':
            from handlers.tester import handle_tester_worker
            return handle_tester_worker(task, es_id)
        if worker['role'] == 'reviewer':
            from handlers.reviewer import handle_reviewer_worker
            return handle_reviewer_worker(task, es_id)
        if worker['role'] in ('intake', 'memory-updater'):
            log(f"{worker['role']} worker heartbeat")
            return True
    except Exception as e:
        import traceback
        # Q1: Log full stack trace and clear any stale claim so tasks recover
        log(f"worker {worker.get('name', 'unknown')} error: {e}\n{traceback.format_exc()}")
        task_id = worker.get('current_task_id')
        if task_id:
            try:
                es_id, _ = fetch_task_doc(task_id)
                if es_id:
                    role = worker.get('role', '')
                    fallback_status = 'review' if role in ('tester', 'reviewer') else 'ready'
                    update_task_doc(es_id, {
                        'status': fallback_status,
                        **_implementer_clear_claim_fields(),
                    })
                    log(f"cleared stale claim on task={task_id} after worker crash — resetting status to {fallback_status}")
            except Exception:
                pass  # Best-effort cleanup
        return False
    return True


def main():
    apply_runtime_config(_WS)
    from flume_secrets import hydrate_secrets_from_openbao
    hydrate_secrets_from_openbao()
        
    if 'https' in os.environ.get("ES_URL", "") and (not os.environ.get("ES_API_KEY") or os.environ.get("ES_API_KEY") == 'AUTO_GENERATED_BY_INSTALLER'):
        if not os.environ.get("FLUME_ELASTIC_PASSWORD"):
            raise SystemExit(
                'ES_API_KEY or FLUME_ELASTIC_PASSWORD is required for TLS clusters. Use OpenBao KV (secret/flume) or .env'
            )
    target_worker = sys.argv[1] if len(sys.argv) > 1 else None
    if target_worker:
        log(f'worker handler spawned targeting explicitly [{target_worker}]')
    else:
        log('worker handlers starting in fallback general mode')
        
    while True:
        try:
            apply_runtime_config(_WS)
            sync_llm_env_from_workspace(resolve_safe_workspace())
            
            NODE_ID = os.environ.get('HOSTNAME') or socket.gethostname() or "null-node"
            try:
                node_doc = es_request(f'/agent-system-workers/_doc/{NODE_ID}', method='GET')
                state = node_doc.get('_source', {}) if (node_doc and '_source' in node_doc) else {'workers': []}
            except Exception as es_err:
                # Catch 404s or connection refused securely without crashing the daemon loop
                log(f"ES worker telemetry fetch failed: {es_err}")
                state = {'workers': []}
                
            claimed_workers = {w.get('name') for w in state.get('workers', []) if w.get('status') == 'claimed'}

            # Release orphaned tasks: active_worker set but no matching claimed worker
            if not target_worker:
                try:
                    res = es_request(
                        f'/{TASK_INDEX}/_search',
                        {'size': 500, 'query': {'bool': {'must': [{'match': {'queue_state': 'active'}}]}}},
                        method='POST',
                    )
                    for h in res.get('hits', {}).get('hits', []):
                        src = h.get('_source', {})
                        aw = src.get('active_worker')
                        if aw and aw not in claimed_workers:
                            # Route to the status the owning role will claim
                            # from. pm -> planned, tester/reviewer -> review,
                            # else -> ready. Without this check, reviewer-owned
                            # tasks get silently orphaned in `ready` state.
                            owner = (src.get('owner') or src.get('assigned_agent_role') or 'implementer')
                            owner = owner.strip().lower()
                            if owner not in ('implementer', 'tester', 'reviewer', 'pm', 'intake', 'memory-updater'):
                                owner = 'implementer'
                            if owner == 'pm':
                                recover_status = 'planned'
                            elif owner in ('tester', 'reviewer'):
                                recover_status = 'review'
                            else:
                                recover_status = 'ready'
                            update_task_doc(h.get('_id'), {
                                'status': recover_status,
                                'owner': owner,
                                'assigned_agent_role': owner,
                                'queue_state': 'queued',
                                'active_worker': None,
                                'needs_human': False,
                            })
                            log(f"released orphaned task={src.get('id')} (active_worker={aw}) -> {recover_status}/{owner}")
                except BaseException as cleanup_err:
                    log(f"Orphan task cleanup failed securely: {cleanup_err}")

            for worker in state.get('workers', []):
                if worker.get('status') == 'claimed':
                    if target_worker and worker.get('name') != target_worker:
                        continue
                    # Safety: if a task is stuck in running with no agent_log, release it
                    try:
                        task_id = worker.get('current_task_id')
                        role = worker.get('role')
                        if task_id and role in ('implementer', 'tester', 'reviewer'):
                            es_id, task = fetch_task_doc(task_id)
                            if es_id and task and not task.get('agent_log'):
                                # Safety trap disabled: Tasks are just claimed, agent_log is natively empty!
                                # patch = { ... }
                                # update_task_doc(es_id, patch)
                                # log(f"{role}: released task={task_id} (no agent_log)")
                                # continue
                                pass
                    except Exception:
                        pass
                    log(f"Executing run_worker for {worker.get('name')} targeting task={worker.get('current_task_id')}")
                    run_worker(worker)
                    log(f"Completed run_worker for {worker.get('name')}")
            time.sleep(POLL_SECONDS)
        except Exception as e:
            log(f'handler loop error: {e}')
            time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
