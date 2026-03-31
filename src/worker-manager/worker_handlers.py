#!/usr/bin/env python3
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request

import uuid
import socket
from datetime import datetime, timezone
from pathlib import Path

NODE_ID = os.environ.get('HOSTNAME') or socket.gethostname() or "null-node"

_WS = Path(__file__).resolve().parent.parent
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))
from flume_secrets import apply_runtime_config  # noqa: E402
from workspace_llm_env import sync_llm_env_from_workspace  # noqa: E402

apply_runtime_config(_WS)

from agent_runner import run_implementer, run_pm_dispatcher, run_reviewer, run_tester

BASE = _WS / 'worker-manager'
from utils.workspace import resolve_safe_workspace

ES_URL = os.environ.get('ES_URL', 'http://127.0.0.1:9200').rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY', '')
ES_VERIFY_TLS = os.environ.get('ES_VERIFY_TLS', 'false').lower() == 'true'
TASK_INDEX = os.environ.get('ES_INDEX_TASKS', 'agent-task-records')
HANDOFF_INDEX = os.environ.get('ES_INDEX_HANDOFFS', 'agent-handoff-records')
FAILURE_INDEX = os.environ.get('ES_INDEX_FAILURES', 'agent-failure-records')
REVIEW_INDEX = os.environ.get('ES_INDEX_REVIEWS', 'agent-review-records')
PROVENANCE_INDEX = os.environ.get('ES_INDEX_PROVENANCE', 'agent-provenance-records')
POLL_SECONDS = int(os.environ.get('WORKER_MANAGER_POLL_SECONDS', '15'))
PROJECTS_REGISTRY = _WS / 'projects.json'

ctx = None
if not ES_VERIFY_TLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE


def now_iso():
    return datetime.now(timezone.utc).isoformat()


from utils.logger import get_logger

try:
    _log_dir_env = os.environ.get('FLUME_LOG_DIR', '').strip()
    from utils.workspace import resolve_safe_workspace
    _log_dir = Path(_log_dir_env).resolve() if _log_dir_env else resolve_safe_workspace() / 'logs'
    _log_dir.mkdir(parents=True, exist_ok=True)
    _handlers_logger = get_logger('worker-handlers', file_path=str(_log_dir / f'worker_handlers_{NODE_ID}.log'))
except Exception as e:
    _handlers_logger = get_logger('worker-handlers')
    _handlers_logger.error(f"Failed to provision JSON Rotating File Handler on handlers node: {e}")

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


def update_task_doc(es_id, doc):
    doc['updated_at'] = now_iso()
    doc['last_update'] = now_iso()
    es_request(f'/{TASK_INDEX}/_update/{es_id}', {'doc': doc}, method='POST')


def write_doc(index, doc):
    es_request(f'/{index}/_doc', doc, method='POST')


def append_agent_note(es_id: str, note: str) -> None:
    """Append a live progress note to the task's agent_log field (capped at 100 entries)."""
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


def load_project_repo_path(repo_id):
    if not repo_id or not PROJECTS_REGISTRY.exists():
        return None
    try:
        raw = json.loads(PROJECTS_REGISTRY.read_text())
        entries = raw.get('projects') if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return None
        for p in entries:
            if not isinstance(p, dict):
                continue
            if p.get('id') == repo_id:
                path = p.get('path')
                return Path(path) if path else None
    except Exception:
        return None
    return None


def ensure_task_branch(task):
    """Create/switch to a per-task branch for implementer work and return branch name/path using Native Git Worktrees."""
    task_id = task.get('id') or 'task'
    repo_id = task.get('repo')
    repo_path = load_project_repo_path(repo_id)
    if not repo_path or not (repo_path / '.git').exists():
        return None, None

    item_type = (task.get('item_type') or '').lower()
    prefix = 'bugfix' if item_type == 'bug' or task_id.startswith('bug-') else 'feature'
    parent_id = (task.get('parent_id') or '').lower()
    branch_key = task_id
    if item_type == 'task' and parent_id.startswith('story-'):
        branch_key = parent_id

    safe_task_id = ''.join(ch if ch.isalnum() or ch in ('-', '_', '/') else '-' for ch in branch_key).strip('-')
    branch = f"{prefix}/{safe_task_id}"
    
    # OS native topology: Mount sandboxes adjacent to original repo avoiding concurrent collisions
    worktree_mgr_path = repo_path.parent / f"{repo_path.name}-worktrees"
    worktree_path = worktree_mgr_path / task_id
    
    try:
        if not worktree_mgr_path.exists():
            os.makedirs(worktree_mgr_path, exist_ok=True)
            
        if worktree_path.exists() and (worktree_path / '.git').exists():
            return branch, str(worktree_path)

        # Check if the branch exists locally across the swarm
        proc = subprocess.run(['git', '-C', str(repo_path), 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}'])
        branch_exists = (proc.returncode == 0)
        
        if branch_exists:
            subprocess.run(['git', '-C', str(repo_path), 'worktree', 'add', str(worktree_path), branch], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(['git', '-C', str(repo_path), 'worktree', 'add', str(worktree_path), '-b', branch], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
        return branch, str(worktree_path)
    except Exception as e:
        log(f"worktree setup failed for task={task_id}: {e}")
        return None, str(repo_path)


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
    """Load gitflow config for a project."""
    if not repo_id or not PROJECTS_REGISTRY.exists():
        return {'autoPrOnApprove': True, 'defaultBranch': None}
    try:
        raw = json.loads(PROJECTS_REGISTRY.read_text())
        entries = raw.get('projects') if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return {'autoPrOnApprove': True, 'defaultBranch': None}
        for p in entries:
            if not isinstance(p, dict):
                continue
            if p.get('id') == repo_id:
                return p.get('gitflow') or {'autoPrOnApprove': True, 'defaultBranch': None}
    except Exception:
        pass
    return {'autoPrOnApprove': True, 'defaultBranch': None}


def create_pr_for_task(task, reviewer_model):
    """
    Create a GitHub PR using `gh pr create`.
    Writes a provenance note and returns (pr_url, pr_number, error).
    """
    task_id = task.get('id', 'unknown')
    branch = task.get('branch')
    if not branch:
        log(f"create_pr: no branch on task={task_id}, skipping")
        return None, None, 'no_branch'

    repo_id = task.get('repo')
    repo_path = load_project_repo_path(repo_id)
    if not repo_path or not (repo_path / '.git').exists():
        log(f"create_pr: repo path not found or not a git repo for task={task_id}")
        return None, None, 'no_repo'

    gitflow = load_project_gitflow(repo_id)
    target_branch = resolve_default_branch(str(repo_path), override=gitflow.get('defaultBranch'))

    # Idempotency: if a PR already exists for this head branch, reuse it
    # instead of creating duplicates.
    try:
        list_res = subprocess.run(
            ['gh', 'pr', 'list',
             '--head', branch,
             '--base', target_branch,
             '--state', 'open',
             '--json', 'url,number',
             '--limit', '1'],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if list_res.returncode == 0 and list_res.stdout.strip():
            import json
            arr = json.loads(list_res.stdout)
            if arr:
                existing = arr[0]
                return existing.get('url'), existing.get('number'), None
    except Exception:
        pass

    title = task.get('title') or f"Task {task_id}"
    ac = task.get('acceptance_criteria') or []
    ac_lines = '\n'.join(f'- {c}' for c in ac) if ac else '_None recorded_'
    commit_sha = task.get('commit_sha') or ''
    sha_line = f'\n\n**Commit:** `{commit_sha}`' if commit_sha else ''
    body = (
        f"## {title}\n\n"
        f"**Task ID:** `{task_id}`\n"
        f"**Repo:** `{repo_id}`\n"
        f"**Branch:** `{branch}` → `{target_branch}`\n"
        f"**Model:** `{reviewer_model}`\n"
        f"{sha_line}\n\n"
        f"### Acceptance Criteria\n{ac_lines}\n\n"
        f"_Auto-generated by OpenClaw agent workflow._"
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

    pr_url = result.stdout.strip()
    pr_number = None
    url_parts = pr_url.rstrip('/').split('/')
    if url_parts and url_parts[-1].isdigit():
        pr_number = int(url_parts[-1])

    log(f"create_pr: PR created for task={task_id} -> {pr_url}")
    return pr_url, pr_number, None


def create_bug_task(parent_task, bug, idx):
    bug_id = f"bug-{parent_task.get('id', 'task')}-{idx}"
    doc = {
        'id': bug_id,
        'title': bug.get('title', f"Bug follow-up for {parent_task.get('title', 'task')}"),
        'objective': bug.get('objective', 'Fix defect identified during tester validation.'),
        'repo': parent_task.get('repo'),
        'worktree': parent_task.get('worktree'),
        'item_type': 'bug',
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
    # Process from leaf parents up (epics last) using type ordering
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
        all_done = all(by_id.get(cid, {}).get('status') == 'done' for cid in child_ids)
        if all_done:
            update_task_doc(src['_es_id'], {'status': 'done'})
            src['status'] = 'done'  # update local view for subsequent passes
            changed += 1
            log(f"compute_ready: marked parent {parent_id} ({src.get('item_type')}) done — all children done")

    return changed


def handle_pm_dispatcher_worker(task):
    if not task:
        return True

    task_id = task.get('id')
    es_id, _ = fetch_task_doc(task_id) if task_id else (None, None)

    # Intelligent Task Scope & PM Hallucination Boundaries
    active_model = task.get('preferred_model', 'gpt-4o').lower()
    if 'gpt-4' in active_model or 'claude-3-opus' in active_model:
        # High capacity models: chunk into component-level epics
        task['chunking_strategy'] = 'epic_component_level'
    else:
        # Smaller models / local inferences: 20-line recursive functional scopes
        task['chunking_strategy'] = '20_line_functional_scope'

    try:
        result = run_pm_dispatcher(task)
    except Exception as e:
        log(f"pm-dispatcher: Execution Trap mapping decomposition on {task_id} natively: {e}")
        if es_id:
            update_task_doc(es_id, {
                'status': 'blocked',
                'active_worker': None,
                'queue_state': 'queued',
            })
        return True

    if result.action == 'decompose' and getattr(result, 'subtasks', []):
        count = 0
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
            count += 1

        if es_id:
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
        update_task_doc(es_id, {
            'active_worker': None,
            'queue_state': 'queued',
        })
    return True


def auto_commit_and_push(repo_path: str, branch: str, commit_message: str, task_id: str) -> str:
    """Stage all changes, commit, push, and return the new SHA. Returns '' on failure."""
    try:
        status = subprocess.run(
            ['git', '-C', repo_path, 'status', '--porcelain'],
            capture_output=True, text=True,
        )
        if not status.stdout.strip():
            log(f"auto_commit: no changes to commit for task={task_id}")
            return ''

        # Some environments (containers/CI) don't have git identity configured.
        # Ensure commits don't fail with "Author identity unknown".
        email = subprocess.run(
            ['git', '-C', repo_path, 'config', 'user.email'],
            capture_output=True, text=True,
        )
        if email.returncode != 0 or not (email.stdout or '').strip():
            subprocess.run(
                ['git', '-C', repo_path, 'config', 'user.email', 'ai-bot@local'],
                check=True, capture_output=True, text=True,
            )
        name = subprocess.run(
            ['git', '-C', repo_path, 'config', 'user.name'],
            capture_output=True, text=True,
        )
        if name.returncode != 0 or not (name.stdout or '').strip():
            subprocess.run(
                ['git', '-C', repo_path, 'config', 'user.name', 'AI Bot'],
                check=True, capture_output=True, text=True,
            )

        subprocess.run(['git', '-C', repo_path, 'add', '-A'], check=True, capture_output=True)
        subprocess.run(
            ['git', '-C', repo_path, 'commit', '-m', commit_message or f'Implement task {task_id}'],
            check=True, capture_output=True,
        )
        subprocess.run(
            ['git', '-C', repo_path, 'push', '-u', 'origin', branch],
            check=True, capture_output=True, timeout=60,
        )
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
    for base in ('origin/main', 'origin/master', 'main', 'master'):
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
    # The `objective` field is sometimes inherited from the parent story and
    # can be too generic (e.g., "Update footer text..."), so we key off the
    # *title* which is specific to whether this is a replace/modify vs verify/test
    # step.
    text = f"{task.get('title', '')}".lower()
    # Code-edit verbs (keep this list conservative to avoid flagging pure
    # validation tasks like "verify"/"validate"/"test").
    code_triggers = [
        'replace', 'update', 'modify', 'implement', 'write', 'add ', 'remove ',
        'create', 'change', 'set ', 'edit ',
    ]
    return any(t in text for t in code_triggers)


def _implementer_clear_claim_fields() -> dict:
    """Drop manager claim markers so the task is not stuck 'running' in the UI."""
    return {'active_worker': None, 'queue_state': 'queued'}


def _implementer_max_llm_failures_cap() -> int:
    """
    Stop infinite ready→running loops when the LLM never returns a usable response.
    After this many consecutive failures the task is blocked for human attention.
    Set FLUME_IMPLEMENTER_MAX_LLM_FAILURES=0 to disable (previous behavior).
    """
    raw = os.environ.get('FLUME_IMPLEMENTER_MAX_LLM_FAILURES', '0').strip()
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
    # Guarantee absolute node isolation immediately via Worktree Sandboxing
    branch, worktree_path = ensure_task_branch(task)
    repo_path = worktree_path or str(load_project_repo_path(task.get('repo')))

    # Clear any stale notes from previous runs, then write live progress to ES
    try:
        es_request(f'/{TASK_INDEX}/_update/{es_id}', {'doc': {'agent_log': []}}, method='POST')
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

    # Hint to the agent and enforce worker-side gating.
    task['requires_code'] = task_requires_code(task)
    task_id = task.get('id', '')
    released = False

    try:
        result = run_implementer(task, repo_path=repo_path, on_progress=_on_progress)
        implementer_model = task.get('preferred_model') or os.environ.get('LLM_MODEL', 'llama3.2')

        if result.metadata.get('source') == 'llm_no_response':
            _implementer_handle_llm_failure(es_id, task, task_id)
            released = True
            return True

        commit_message = result.metadata.get('commit_message') or f"Implement task: {task.get('title', task_id)}"
        commit_sha = ''
        branch = None
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

            # Edge case: branch already had commits from a previous partial run
            if not commit_sha and branch and _branch_has_new_commits(repo_path, branch):
                existing_sha, existing_msg = get_latest_commit_sha(repo_path)
                commit_sha = existing_sha
                if not commit_message:
                    commit_message = existing_msg

            # If the agent *should* have written code (has_changes=True) but we couldn't
            # record it (commit_sha == ''), don't mark the task done.
            if has_changes and not commit_sha:
                update_task_doc(es_id, {
                    'status': 'blocked',
                    'needs_human': True,
                    'owner': 'implementer',
                    'assigned_agent_role': 'implementer',
                    **_implementer_clear_claim_fields(),
                })
                log(f"implementer: task={task_id} has changes but commit failed; blocking for human attention")
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
                # Re-queue rather than incorrectly marking the task done.
                update_task_doc(es_id, {
                    'status': 'ready',
                    'owner': 'implementer',
                    'assigned_agent_role': 'implementer',
                    'needs_human': False,
                    **_implementer_clear_claim_fields(),
                })
                log(f"implementer: task={task_id} expected code edits but wrote nothing; re-queued")
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
        update_task_doc(es_id, {
            'status': 'blocked',
            'needs_human': True,
            'owner': 'implementer',
            'assigned_agent_role': 'implementer',
        })
        log(f"tester: task={task.get('id')} has no commit_sha — blocking, implementer must make real changes first")
        return True

    result = run_tester(task)
    tester_model = task.get('preferred_model') or os.environ.get('LLM_MODEL', 'llama3.2')
    if result.action == 'fail':
        bugs = result.bugs or [{
            'title': f"Bug follow-up for {task.get('title', task.get('id'))}",
            'objective': result.summary,
            'severity': 'high',
        }]
        for idx, bug in enumerate(bugs, start=1):
            create_bug_task(task, bug, idx)
        update_task_doc(es_id, {
            'status': 'ready',
            'owner': 'implementer',
            'assigned_agent_role': 'implementer',
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
        'task_id': task.get('id'),
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
    update_task_doc(es_id, {
        'status': 'review',
        'owner': 'reviewer',
        'assigned_agent_role': 'reviewer',
    })
    log(f"tester passed task={task.get('id')} -> reviewer")
    return True


def handle_reviewer_worker(task, es_id):
    # Gate: don't approve if no real commit was recorded by the implementer
    if not task.get('commit_sha'):
        update_task_doc(es_id, {
            'status': 'blocked',
            'needs_human': True,
            'owner': 'implementer',
            'assigned_agent_role': 'implementer',
        })
        log(f"reviewer: task={task.get('id')} has no commit_sha — blocking, implementer must make real changes first")
        return True

    result = run_reviewer(task)
    reviewer_model = task.get('preferred_model') or os.environ.get('LLM_MODEL', 'llama3.2')
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
        update_task_doc(es_id, {'status': 'done', 'owner': 'reviewer'})
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
                pr_url, pr_number, pr_error = create_pr_for_task(task, reviewer_model)
                if pr_url:
                    update_task_doc(es_id, {
                        'pr_url': pr_url,
                        'pr_number': pr_number,
                        'pr_status': 'open',
                        'target_branch': resolve_default_branch(
                            str(load_project_repo_path(task.get('repo')) or ''),
                            override=gitflow.get('defaultBranch'),
                        ),
                    })
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
        update_task_doc(es_id, {
            'status': 'ready',
            'owner': 'implementer',
            'assigned_agent_role': 'implementer',
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
    update_task_doc(es_id, {'status': 'blocked', 'needs_human': True, 'owner': 'reviewer'})
    log(f"reviewer blocked task={task_id}")
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
        log(f"worker {worker['name']} error: {e}")
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
                    run_worker(worker)
            time.sleep(POLL_SECONDS)
        except Exception as e:
            log(f'handler loop error: {e}')
            time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
