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
import urllib.parse
import urllib.request

import uuid
import socket
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

from agent_runner import run_implementer, run_pm_dispatcher, run_reviewer, run_tester  # noqa: E402  # type: ignore

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
    headers = {'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}'}
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


class KillSwitchAbortError(Exception):
    pass


def check_kill_switch(es_id: str):
    """Enforce native state bounding to synchronously interrupt stray execution loops."""
    try:
        res = es_request(f'/{TASK_INDEX}/_doc/{es_id}?_source=status')
        if res.get('_source', {}).get('status') == 'blocked':
            _handlers_logger.warning("Kill Switch Engaged: Worker thread aborting immediately for blocked task.")
            raise KillSwitchAbortError("Task was halted via Kill Switch")
    except KillSwitchAbortError as e:
        raise e
    except Exception as e:
        _handlers_logger.debug(f"Non-fatal failure checking kill switch for {es_id}: {e}")


def update_task_doc(es_id, doc):
    check_kill_switch(es_id)
    doc['updated_at'] = now_iso()
    doc['last_update'] = now_iso()
    
    # Dual-Write CQRS Materialization: Emit immutable event before updating in-place Materialized View
    emit_task_event(es_id, 'doc_update', doc)
    
    es_request(f'/{TASK_INDEX}/_update/{es_id}', {'doc': doc}, method='POST')


def write_doc(index, doc):
    es_request(f'/{index}/_doc', doc, method='POST')


def append_agent_note(es_id: str, note: str) -> None:
    """Append a live progress note to the task's agent_log field (capped at 100 entries)."""
    check_kill_switch(es_id)
    try:
        ts = now_iso()
        es_request(f'/{TASK_INDEX}/_update/{es_id}', {
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
                'source': (
                    'if (ctx._source.execution_thoughts == null) { ctx._source.execution_thoughts = []; }'
                    'ctx._source.execution_thoughts.add(params.entry);'
                    'if (ctx._source.execution_thoughts.length > 500) { ctx._source.execution_thoughts.remove(0); }'
                    'ctx._source.updated_at = params.touch;'
                    'ctx._source.last_update = params.touch;'
                ),
                'lang': 'painless',
                'params': {'entry': {'ts': ts, 'thought': thought}, 'touch': ts},
            }
        }, method='POST')
    except Exception as e:
        log(f"[worker_handlers] append_execution_thought error: {e}")


def _es_projects_request_worker(path: str, body=None, method: str = "GET") -> dict:
    """Lightweight ES request helper scoped to flume-projects index (no httpx dep)."""
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("ES_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
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
            branch = f"{prefix}/{task_id}-{uuid.uuid4().hex[:6]}"

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
    gitflow = load_project_gitflow(repo_id)
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
    """
    if not pr_number or task.get('release_promotion_task'):
        return
    repo_id = task.get('repo')
    gf = load_project_gitflow(repo_id)
    if not gf.get('autoMergeIntegrationPr', True):
        return
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


def create_bug_task(parent_task, bug, idx):
    parent_id = parent_task.get('id', 'task')
    bug_id = f"bug-{parent_id}-{idx}"
    doc = {
        'id': bug_id,
        'title': bug.get('title', f"Bug follow-up for {parent_task.get('title', 'task')}"),
        'objective': bug.get('objective', 'Fix defect identified during tester validation.'),
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
        'priority': 'high' if bug.get('severity') == 'high' else 'normal',
        'depends_on': [],
        'acceptance_criteria': [],
        'artifacts': [],
        'needs_human': False,
        'risk': 'medium',
        'created_at': now_iso(),
        'updated_at': now_iso(),
        'last_update': now_iso(),
    }
    write_doc(TASK_INDEX, doc)


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
            if not src.get('commit_sha') and deps:
                dep = by_id.get(deps[0])
                if dep and dep.get('commit_sha'):
                    patch['commit_sha'] = dep.get('commit_sha')
                    patch['commit_message'] = dep.get('commit_message')
                    patch['branch'] = dep.get('branch')
                    patch['worktree'] = dep.get('worktree')
        update_task_doc(src['_es_id'], patch)
        src.update(patch)  # update local view
        changed += 1
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


def handle_pm_dispatcher_worker(task):
    if not task:
        return True

    task_id = task.get('id')
    es_id, _ = fetch_task_doc(task_id) if task_id else (None, None)

    # Initialize execution_thoughts for this run so the drawer can display live reasoning
    if es_id:
        try:
            es_request(f'/{TASK_INDEX}/_update/{es_id}', {'doc': {'execution_thoughts': []}}, method='POST')
        except Exception:
            pass
        append_execution_thought(es_id, f"*[PM Dispatcher]* Analyzing task: **{task.get('title', task_id)}**")

    # Intelligent Task Scope & PM Hallucination Boundaries
    active_model = str(task.get('preferred_model') or _get_active_llm_model()).lower()
    if 'gpt-4' in active_model or 'claude-3-opus' in active_model:
        # High capacity models: chunk into component-level epics
        task['chunking_strategy'] = 'epic_component_level'
    else:
        # Smaller models / local inferences: 20-line recursive functional scopes
        task['chunking_strategy'] = '20_line_functional_scope'

    if es_id:
        append_execution_thought(es_id, f"*[PM Dispatcher]* Sending to LLM for decomposition analysis (model: `{active_model}`)…")

    try:
        result = run_pm_dispatcher(task)
    except Exception as e:
        log(f"pm-dispatcher: Execution Trap mapping decomposition on {task_id} natively: {e}")
        if es_id:
            append_execution_thought(es_id, f"*[PM Dispatcher]* ❌ Decomposition failed: {str(e)[:200]}")
            update_task_doc(es_id, {
                'status': 'blocked',
                'active_worker': None,
                'queue_state': 'queued',
            })
        return True

    if result.action == 'decompose' and getattr(result, 'subtasks', []):
        count = 0
        child_titles = []
        for st in result.subtasks:
            child_id = f"{st.get('item_type', 'task')}-{uuid.uuid4().hex[:8]}"
            doc = {
                'id': child_id,
                'parent_id': task_id,
                'title': st.get('title', 'Generated Subtask'),
                'objective': st.get('objective', ''),
                'item_type': st.get('item_type', 'task'),
                'repo': task.get('repo'),
                'status': 'planned',
                'owner': 'pm',
                'assigned_agent_role': 'pm',
                'depends_on': [],
                'acceptance_criteria': [],
                'artifacts': [],
                'needs_human': False,
                'created_at': now_iso(),
                'updated_at': now_iso(),
                'last_update': now_iso(),
            }
            write_doc(TASK_INDEX, doc)
            child_titles.append(st.get('title', child_id))
            count += 1

        if es_id:
            subtask_list = "\n".join(f"  - {t}" for t in child_titles)
            append_execution_thought(es_id, f"*[PM Dispatcher]* ✅ Decomposed into **{count}** children:\n{subtask_list}")
            update_task_doc(es_id, {
                'status': 'running',
                'active_worker': None,
                'queue_state': 'queued',
            })
            log(f"pm-dispatcher: decomposed {task_id} into {count} children; suspended parent.")
        return True

    promoted = compute_ready_for_repo((task or {}).get('repo'))
    log(f"pm-dispatcher: {result.summary[:200]}; promoted={promoted}")
    
    if es_id:
        append_execution_thought(es_id, f"*[PM Dispatcher]* Task is compute-ready. Summary: {result.summary[:300]}")
        update_task_doc(es_id, {
            'active_worker': None,
            'queue_state': 'queued',
        })
    return True



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
        append_agent_note(
            es_id,
            f'Blocked: implementer hit {next_n} consecutive LLM failures (cap={cap}, '
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
        result = run_implementer(task, repo_path=repo_path, on_progress=_on_progress, on_thought=_on_thought)
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
                    append_agent_note(
                        es_id,
                        f'Blocked: git push failed {next_push_failures} consecutive times (cap={_MAX_PUSH_FAILURES}). '
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
        # teardown_task_clone() is a no-op for local repos and non-tmp paths.
        teardown_task_clone(worktree_path)
        if not released:
            try:
                _, cur = fetch_task_doc(task_id)
                if cur and str(cur.get('status') or '') == 'running':
                    update_task_doc(es_id, {
                        'status': 'ready',
                        'owner': 'implementer',
                        'assigned_agent_role': 'implementer',
                        'needs_human': False,
                        **_implementer_clear_claim_fields(),
                    })
                    log(f"implementer: released stuck running task={task_id} (handler did not finish normally)")
            except Exception as ex:
                log(f"implementer: finally guard failed task={task_id}: {ex}")


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
        append_agent_note(
            es_id,
            f'Blocked: tester has looped {next_retries} times without the reviewer completing '
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

    result = run_tester(task)
    tester_model = task.get('preferred_model') or _get_active_llm_model()

    if result.action == 'fail':
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
            'recurrence_count': 1,
            'created_at': now_iso(),
            'updated_at': now_iso(),
        })
        log(f"tester found bugs for task={task.get('id')} and re-queued implementer")
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

    result = run_reviewer(task)
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
        # B3: Clear claim fields so the task is re-claimable by another worker
        update_task_doc(es_id, {
            'status': 'ready',
            'owner': 'implementer',
            'assigned_agent_role': 'implementer',
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
            'recurrence_count': 1,
            'created_at': now_iso(),
            'updated_at': now_iso(),
        })
        log(f"reviewer requested changes for task={task_id}")
        return True

    # Unknown verdict (e.g. hallucinated value not caught by agent_runner normalisation).
    # Re-queue to the reviewer for another attempt, rather than permanently blocking.
    # If the reviewer has already looped too many times, escalate to human.
    _REVIEWER_BLOCK_CAP = int(os.environ.get('FLUME_REVIEWER_BLOCK_CAP', '3'))
    prev_blocks = int(task.get('reviewer_block_count', 0))
    next_blocks = prev_blocks + 1

    if next_blocks >= _REVIEWER_BLOCK_CAP:
        append_agent_note(
            es_id,
            f'Blocked: reviewer returned an unresolvable verdict {next_blocks} times '
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


def run_worker(worker):
    try:
        os.environ['FLUME_WORKER_NAME'] = worker.get('name', 'unknown')
        os.environ['FLUME_WORKER_ROLE'] = worker.get('role', 'unknown')
        task_id = worker.get('current_task_id')
        if not task_id and worker.get('role') != 'pm':
            return True
        es_id, task = fetch_task_doc(task_id) if task_id else (None, None)
        if worker['role'] == 'pm':
            return handle_pm_dispatcher_worker(task)
        if not es_id or not task:
            log(f"worker {worker['name']} could not find task={task_id}")
            return False
        if worker['role'] == 'implementer':
            return handle_implementer_worker(task, es_id)
        if worker['role'] == 'tester':
            return handle_tester_worker(task, es_id)
        if worker['role'] == 'reviewer':
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
        raise SystemExit(
            'ES_API_KEY is required for TLS clusters. Use OpenBao KV (secret/flume) or .env'
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
                            update_task_doc(h.get('_id'), {
                                'status': 'ready',
                                'queue_state': 'queued',
                                'active_worker': None,
                                'needs_human': False,
                            })
                            log(f"released orphaned task={src.get('id')} (active_worker={aw})")
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
