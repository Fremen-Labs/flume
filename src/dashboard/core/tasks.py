import os
import json
import subprocess
from utils.async_subprocess import run_cmd_async
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.logger import get_logger
from core.elasticsearch import es_search, es_post
from core.projects_store import load_projects_registry



logger = get_logger(__name__)


async def delete_task_branches(ids: list, repo: str) -> list:
    """
    For any tasks in `ids` that have a `branch` field, delete that git branch
    from the local repository (and remote origin if it exists).
    Returns a list of branch names that were successfully deleted.
    """
    query_must: list = [
        {'terms': {'id': ids}},
        {'exists': {'field': 'branch'}},
    ]
    if repo:
        query_must.append({'term': {'repo': repo}})

    try:
        hits = es_search('agent-task-records', {
            'size': 500,
            '_source': ['id', 'repo', 'branch'],
            'query': {'bool': {'must': query_must}},
        }).get('hits', {}).get('hits', [])
    except Exception:
        return []

    registry = load_projects_registry()
    deleted = []

    # If multiple tasks share the same branch (e.g., tasks under the same
    # story), we must not delete the shared branch until no ES records
    # remain for it.
    ids_set = set(ids or [])

    for h in hits:
        src = h.get('_source') or {}
        branch = (src.get('branch') or '').strip()
        repo_id = src.get('repo', '')
        if not branch or not repo_id:
            continue

        proj = next((p for p in registry if p['id'] == repo_id), None)
        if not proj:
            continue

        # AP-12: Only local-path projects have a persistent repo on disk.
        # Remote/indexed projects have no local clone — skip git branch ops.
        local_path = proj.get('path') or ''
        if not local_path or proj.get('clone_status') not in ('local',):
            if not local_path:
                logger.debug(json.dumps({'event': 'ap12_skip_non_local_branch_delete', 'repo_id': repo_id, 'clone_status': proj.get('clone_status')}))
            continue
        repo_path = Path(local_path)
        if not (repo_path / '.git').exists():
            continue

        # Shared-branch safety: if any other remaining task doc still uses
        # this branch, skip deletion.
        try:
            remaining = es_search('agent-task-records', {
                'size': 1,
                '_source': ['id'],
                'query': {
                    'bool': {
                        'must': [
                            {'term': {'repo': repo_id}},
                            {'term': {'branch': branch}},
                        ],
                        'must_not': [{'terms': {'id': list(ids_set)}}],
                    }
                },
            }).get('hits', {}).get('hits', [])
            if remaining:
                continue
        except Exception:
            # Best-effort: if ES check fails, fall back to deleting.
            pass

        # Delete local branch (force, since it may not be merged)
        try:
            rc, out, err = await run_cmd_async("git", "-C", str(repo_path), "branch", "-D", branch, timeout=15)
            class _R:
                returncode = rc
            result = _R()
            if result.returncode == 0:
                deleted.append(branch)
        except Exception as _e:
            logger.error("Ignored exception", exc_info=True)

        # Best-effort: delete remote tracking branch if it exists on origin
        try:
            await run_cmd_async("git", "-C", str(repo_path), "push", "origin", "--delete", branch, timeout=20)
        except Exception as _e:
            logger.error("Ignored exception", exc_info=True)

    return deleted


async def delete_repo_branches(repo_id: str, branches: list, force: bool) -> dict:
    """
    Delete local git branches for a given dashboard repo.

    Safety defaults:
    - Default branch and currently checked-out branch are protected unless `force=True`.
    - If any non-archived tasks reference the branch, deletion is blocked unless `force=True`.
    """
    try:
        raw_branches = [str(b or '').strip() for b in (branches or [])]
        raw_branches = [b for b in raw_branches if b]
        if not raw_branches:
            return {'ok': False, 'error': 'No branches provided', 'deleted': [], 'skipped': []}

        # Allow typical git ref formats like "feature/x", "bugfix-1", "release/1.2.3".
        # Keep this conservative to avoid command injection / ref weirdness.
        invalid = [b for b in raw_branches if not re.match(r'^[A-Za-z0-9._/\-]+$', b)]
        if invalid:
            return {'ok': False, 'error': 'Invalid branch name(s)', 'invalid': invalid}

        registry = load_projects_registry()
        proj = next((p for p in registry if p['id'] == repo_id), None)
        if not proj:
            return {'ok': False, 'error': f'Project "{repo_id}" not found'}

        # AP-12: Explicit local-only guard — no silent workspace fallback.
        local_path = proj.get('path') or ''
        if not local_path or proj.get('clone_status') not in ('local',):
            return {'ok': False, 'error': 'Branch deletion is only supported for locally-mounted repos. Remote repos use GitHostClient.'}
        repo_path = Path(local_path)
        if not (repo_path / '.git').exists():
            return {'ok': False, 'error': 'Repo is not a git repository'}

        # Discover actual local branches so we can report "missing" branches.
        try:
            rc, out, err = await run_cmd_async("git", "-C", str(repo_path), "branch", "--format=%(refname:short)")
            raw = out
            local_branches = [b.strip() for b in raw.splitlines() if b.strip()]
        except Exception:
            local_branches = []

        local_set = set(local_branches)
        missing = [b for b in raw_branches if local_branches and b not in local_set]
        branches_to_consider = [b for b in raw_branches if (not local_branches) or b in local_set]
        if not branches_to_consider:
            return {'ok': True, 'deleted': [], 'skipped': [], 'missing': missing}

        default_branch = await resolve_default_branch(
            repo_path, override=proj.get('gitflow', {}).get('defaultBranch')
        )

        current_branch = None
        try:
            rc, out, err = await run_cmd_async("git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD")
            current_branch = out.strip() if rc == 0 else ""
        except Exception as _e:
            logger.error("Ignored exception", exc_info=True)

        protected = set()
        if not force:
            if default_branch:
                protected.add(default_branch)
            if current_branch:
                protected.add(current_branch)

        # If not forcing, block deleting branches that are referenced by active tasks.
        blocked_by_tasks = set()
        if not force:
            try:
                hits = es_search('agent-task-records', {
                    'size': 500,
                    '_source': ['id', 'repo', 'branch', 'status'],
                    'query': {
                        'bool': {
                            'must': [
                                {'terms': {'branch': branches_to_consider}},
                                {'term': {'repo': repo_id}},
                            ],
                            'must_not': [{'term': {'status': 'archived'}}],
                        }
                    },
                }).get('hits', {}).get('hits', [])

                for h in hits:
                    src = h.get('_source') or {}
                    b = (src.get('branch') or '').strip()
                    if b:
                        blocked_by_tasks.add(b)
            except Exception:
                # If ES isn't available, don't block deletion.
                blocked_by_tasks = set()

        to_delete = []
        skipped = []
        for b in branches_to_consider:
            if b in protected:
                skipped.append({'branch': b, 'reason': 'protected (default/current) — use force to override'})
                continue
            if b in blocked_by_tasks:
                skipped.append({'branch': b, 'reason': 'referenced by active tasks — use force to override'})
                continue
            to_delete.append(b)

        # If we are deleting the currently checked-out branch, switch away first.
        if current_branch and current_branch in to_delete:
            checkout_branch = None
            for b in local_branches:
                if b != current_branch and b not in to_delete:
                    checkout_branch = b
                    break
            if not checkout_branch:
                for b in local_branches:
                    if b != current_branch:
                        checkout_branch = b
                        break
            if checkout_branch:
                try:
                    await run_cmd_async("git", "-C", str(repo_path), "switch", checkout_branch, timeout=20)
                    current_branch = checkout_branch
                except Exception:
                    # Best-effort only; deletion may still succeed or fail.
                    pass

        deleted = []
        errors = []
        for b in to_delete:
            try:
                del_flag = '-D' if force else '-d'
                rc, out, err = await run_cmd_async("git", "-C", str(repo_path), "branch", del_flag, b, timeout=15)
                class _R:
                    returncode = rc
                    stderr = err.encode("utf-8")
                result = _R()
                if result.returncode == 0:
                    deleted.append(b)
                else:
                    stderr = (result.stderr or b'').decode(errors='replace').strip()
                    errors.append({'branch': b, 'error': stderr[:200] or 'git branch failed'})
            except Exception:
                errors.append({'branch': b, 'error': 'exception during git branch deletion'})

            # Best-effort: delete remote tracking branch if it exists on origin.
            try:
                subprocess.run(
                    ['git', '-C', str(repo_path), 'push', 'origin', '--delete', b],
                    capture_output=True,
                    timeout=20,
                )
            except Exception as _e:
                logger.error("Ignored exception", exc_info=True)

        return {
            'ok': True,
            'default': default_branch,
            'current': current_branch,
            'deleted': deleted,
            'skipped': skipped,
            'missing': missing,
            'errors': errors,
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)[:200], 'deleted': [], 'skipped': []}


def load_workers() -> list:
    workers = []
    try:
        res = es_search('agent-system-workers', {'size': 100, 'sort': [{'updated_at': {'order': 'desc'}}]})
        hits = res.get('hits', {}).get('hits', [])
        now = datetime.now(timezone.utc)
        for h in hits:
            doc = h.get('_source', {})
            node_workers = doc.get('workers', [])
            for w in node_workers:
                w['status'] = w.get('status', 'idle')
                hb_str = w.get('heartbeat_at')
                if hb_str:
                    try:
                        hb = datetime.fromisoformat(hb_str.replace('Z', '+00:00'))
                        diff_sec = (now - hb).total_seconds()
                        if diff_sec > 120:
                            continue  # Garbage collect from UI view
                        if diff_sec > 30:
                            w['status'] = 'offline/terminated'
                    except Exception as _e:
                        logger.error("Ignored exception", exc_info=True)
                workers.append(w)
    except Exception as e:
        logger.error("Error loading workers from ES", extra={"structured_data": {"error": str(e)}})
        return []
        
    try:
        agg_res = es_search('agent-token-telemetry', {
            'size': 0,
            'aggs': {
                'by_worker': {
                    'terms': {'field': 'worker_name.keyword', 'size': 500},
                    'aggs': {
                        'total_input': {'sum': {'field': 'input_tokens'}},
                        'total_output': {'sum': {'field': 'output_tokens'}}
                    }
                },
                'total_elastro_savings': {
                    'sum': {'field': 'savings'}
                }
            }
        })
        buckets = agg_res.get('aggregations', {}).get('by_worker', {}).get('buckets', [])
        totals = {}
        for b in buckets:
            totals[b.get('key')] = {
                'input': int(b.get('total_input', {}).get('value', 0)),
                'output': int(b.get('total_output', {}).get('value', 0))
            }
        for w in workers:
            w['input_tokens'] = totals.get(w['name'], {}).get('input', 0)
            w['output_tokens'] = totals.get(w['name'], {}).get('output', 0)
    except Exception as _e:
        logger.error("Ignored exception", exc_info=True)
        
    return workers


def priority_rank(priority: str) -> int:
    ranks = {'urgent': 0, 'high': 1, 'medium': 2, 'normal': 3, 'low': 4}
    return ranks.get((priority or '').lower(), 99)


def queue_for_repo(repo_id: str):
    hits = es_search('agent-task-records', {
        'size': 500,
        'query': {
            'bool': {
                'must': [
                    {'term': {'repo': repo_id}},
                    {'term': {'status': 'ready'}},
                ],
                'must_not': [{'term': {'status': 'archived'}}],
            }
        },
        'sort': [{'updated_at': {'order': 'asc', 'unmapped_type': 'date'}}],
    }).get('hits', {}).get('hits', [])
    tasks = [{'_id': h.get('_id'), **h.get('_source', {})} for h in hits]
    tasks.sort(key=lambda t: (priority_rank(t.get('priority')), t.get('updated_at') or t.get('last_update') or ''))
    out = []
    for idx, t in enumerate(tasks, start=1):
        out.append({
            '_id': t.get('_id'),
            'id': t.get('id') or t.get('_id'),
            'title': t.get('title'),
            'status': t.get('status'),
            'priority': t.get('priority'),
            'owner': t.get('owner'),
            'assigned_agent_role': t.get('assigned_agent_role') or t.get('owner'),
            'queuePosition': idx,
            'updated_at': t.get('updated_at') or t.get('last_update'),
        })
    return out


def transition_task(task_id: str, status: str, owner=None, needs_human=None):
    es_id, _src = find_task_doc_by_logical_id(task_id)
    if not es_id:
        return None
    doc = {
        'status': status,
        'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'last_update': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    if owner:
        doc['owner'] = owner
        doc['assigned_agent_role'] = owner
    if needs_human is not None:
        doc['needs_human'] = bool(needs_human)
    if status == 'ready':
        doc['implementer_consecutive_llm_failures'] = 0
    es_post(f'agent-task-records/_update/{es_id}', {'doc': doc})
    return {'_id': es_id, 'id': task_id, **doc}


def task_history(task_id: str):
    es_id, src = find_task_doc_by_logical_id(task_id)
    if not src:
        return None
    task = {'_id': es_id, **src}

    events = []

    def infer_model(src, event_type):
        if src.get('model_used'):
            return src.get('model_used')
        role = src.get('agent_role') or src.get('from_role') or task.get('owner') or task.get('assigned_agent_role')
        role = (role or '').lower()
        if role in ('implementer', 'tester', 'e2e-tester'):
            return os.environ.get('LLM_MODEL', 'llama3.2')
        if role in ('reviewer', 'acceptance-reviewer'):
            return os.environ.get('LLM_MODEL', 'llama3.2')
        if role in ('pm', 'pm-dispatcher', 'intake', 'memory-updater'):
            return os.environ.get('LLM_MODEL', 'llama3.2')
        return task.get('preferred_model') or None

    handoffs = es_search('agent-handoff-records', {
        'size': 100,
        'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'term': {'task_id': task_id}},
    }).get('hits', {}).get('hits', [])
    for h in handoffs:
        src = h.get('_source', {})
        commit_note = ''
        if src.get('commit_sha'):
            commit_note = f"commit: {src['commit_sha'][:8]}"
        if src.get('branch'):
            commit_note = f"branch: {src['branch']}" + (f"  {commit_note}" if commit_note else '')
        events.append({
            'type': 'handoff',
            'timestamp': src.get('created_at'),
            'summary': f"{src.get('from_role', 'unknown')} -> {src.get('to_role', 'unknown')}",
            'details': src.get('reason') or '',
            'notes': src.get('objective') or '',
            'discussion': (src.get('constraints') or '') + (' | ' + commit_note if commit_note else ''),
            'modelUsed': infer_model(src, 'handoff'),
            'data': src,
        })

    reviews = es_search('agent-review-records', {
        'size': 100,
        'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'term': {'task_id': task_id}},
    }).get('hits', {}).get('hits', [])
    for h in reviews:
        src = h.get('_source', {})
        events.append({
            'type': 'review',
            'timestamp': src.get('created_at'),
            'summary': f"Verdict: {src.get('verdict', 'unknown')}",
            'details': src.get('summary') or '',
            'notes': src.get('issues') or '',
            'discussion': src.get('recommended_next_role') or '',
            'modelUsed': infer_model(src, 'review'),
            'data': src,
        })

    failures = es_search('agent-failure-records', {
        'size': 100,
        'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'term': {'task_id': task_id}},
    }).get('hits', {}).get('hits', [])
    for h in failures:
        src = h.get('_source', {})
        events.append({
            'type': 'failure',
            'timestamp': src.get('updated_at') or src.get('created_at'),
            'summary': src.get('error_class') or 'failure',
            'details': src.get('summary') or '',
            'notes': src.get('root_cause') or '',
            'discussion': src.get('fix_applied') or '',
            'modelUsed': infer_model(src, 'failure'),
            'data': src,
        })

    provenance = es_search('agent-provenance-records', {
        'size': 100,
        'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'term': {'task_id': task_id}},
    }).get('hits', {}).get('hits', [])
    for h in provenance:
        src = h.get('_source', {})
        git_note = ''
        if src.get('branch'):
            git_note = f"branch: {src['branch']}"
        if src.get('commit_sha'):
            git_note += f"  sha: {src['commit_sha'][:8]}"
        events.append({
            'type': 'provenance',
            'timestamp': src.get('created_at'),
            'summary': f"Role: {src.get('agent_role', 'unknown')}",
            'details': src.get('review_verdict') or '',
            'notes': ', '.join(src.get('artifacts') or []) + (f' | {git_note}' if git_note else ''),
            'discussion': ', '.join(src.get('context_refs') or []),
            'modelUsed': infer_model(src, 'provenance'),
            'data': src,
        })

    # Add git/PR events if present on the task
    if task.get('branch'):
        pr_summary = ''
        if task.get('pr_url'):
            pr_summary = f"PR #{task.get('pr_number') or '?'} ({task.get('pr_status', 'open')}): {task['pr_url']}"
        elif task.get('pr_status') == 'failed':
            pr_summary = f"PR creation failed: {task.get('pr_error', 'unknown error')}"
        events.append({
            'type': 'git',
            'timestamp': task.get('updated_at') or task.get('last_update'),
            'summary': f"Branch: {task['branch']}" + (f" → {task['target_branch']}" if task.get('target_branch') else ''),
            'details': pr_summary,
            'notes': task.get('commit_message') or '',
            'discussion': task.get('commit_sha') or '',
            'modelUsed': None,
            'data': {
                'branch': task.get('branch'),
                'target_branch': task.get('target_branch'),
                'commit_sha': task.get('commit_sha'),
                'commit_message': task.get('commit_message'),
                'pr_url': task.get('pr_url'),
                'pr_number': task.get('pr_number'),
                'pr_status': task.get('pr_status'),
                'pr_error': task.get('pr_error'),
            },
        })

    # Always include current task snapshot as the latest state event
    events.append({
        'type': 'task_state',
        'timestamp': task.get('updated_at') or task.get('last_update'),
        'summary': f"Status: {task.get('status', 'unknown')}",
        'details': f"Owner: {task.get('owner', 'unknown')}",
        'notes': task.get('objective') or '',
        'discussion': f"Priority: {task.get('priority', 'n/a')}",
        'modelUsed': task.get('preferred_model'),
        'data': task,
    })

    events.sort(key=lambda e: e.get('timestamp') or '', reverse=True)

    # Build `history` in the format the frontend expects: [{ts, role, summary}]
    # Newest events first; agent_log entries (live notes) come first when task is running.
    history = []

    # Live agent notes — shown prominently while task is running
    agent_log = task.get('agent_log') or []
    for entry in reversed(agent_log):  # newest first
        history.append({
            'ts': entry.get('ts', ''),
            'role': 'agent',
            'summary': entry.get('note', ''),
            'type': 'agent_note',
        })

    # Structured events from handoffs, reviews, failures, etc.
    for e in events:
        role = {
            'handoff': f"{(e.get('data') or {}).get('from_role', 'agent')} → {(e.get('data') or {}).get('to_role', '')}",
            'review': 'reviewer',
            'failure': 'system',
            'provenance': (e.get('data') or {}).get('agent_role', 'agent'),
            'git': 'git',
            'task_state': 'system',
        }.get(e.get('type', ''), 'agent')
        summary = e.get('summary', '')
        if e.get('details'):
            summary += f' — {e["details"]}'
        history.append({
            'ts': e.get('timestamp', ''),
            'role': role,
            'summary': summary,
            'type': e.get('type', ''),
        })

    return {'task': task, 'events': events, 'history': history, 'agent_log': agent_log}


async def git_repo_info(repo_id, repo_path: Path):
    info = {
        'id': repo_id,
        'path': str(repo_path),
        'exists': repo_path.exists(),
        'is_git': False,
        'current_branch': None,
        'last_commit': None,
    }
    git_dir = repo_path / '.git'
    if not git_dir.exists():
        return info
    info['is_git'] = True
    try:
        rc, out, err = await run_cmd_async("git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD")
        if rc != 0: raise Exception("git error")
        branch = out.strip()
        info['current_branch'] = branch
    except Exception as _e:
        logger.error("Ignored exception", exc_info=True)
    try:
        rc, out, err = await run_cmd_async("git", "-C", str(repo_path), "log", "-1", "--pretty=format:%H%n%an%n%ai%n%s")
        if rc != 0: raise Exception("git error")
        last = out.splitlines()
        if len(last) >= 4:
            info['last_commit'] = {
                'hash': last[0],
                'author': last[1],
                'date': last[2],
                'subject': last[3],
            }
    except Exception as _e:
        logger.error("Ignored exception", exc_info=True)
    return info


async def resolve_default_branch(repo_path: Path, override: Optional[str] = None) -> str:
    """Resolve the default branch for a repo (main/master/etc.)."""
    if override:
        return override
    try:
        # Try origin/HEAD symbolic ref
        rc, out, err = await run_cmd_async("git", "-C", str(repo_path), "symbolic-ref", "refs/remotes/origin/HEAD")
        if rc != 0: raise Exception("git error")
        ref = out.strip()
        # refs/remotes/origin/main -> main
        return ref.split('/')[-1]
    except Exception as _e:
        logger.error("Ignored exception", exc_info=True)
    try:
        # Fallback: check common branch names
        rc, out, err = await run_cmd_async("git", "-C", str(repo_path), "branch", "-r")
        if rc != 0: raise Exception("git error")
        branches_raw = out
        for candidate in ('main', 'master', 'develop', 'trunk'):
            if f'origin/{candidate}' in branches_raw:
                return candidate
    except Exception as _e:
        logger.error("Ignored exception", exc_info=True)
    try:
        rc, out, err = await run_cmd_async("git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD")
        if rc != 0: raise Exception("git error")
        current = out.strip()
        return current or 'main'
    except Exception:
        return 'main'


def get_task_doc(task_id: str):
    """Fetch a single task document from ES by logical id."""
    return find_task_doc_by_logical_id(task_id)


async def create_task_pr(task_id: str) -> dict:
    """
    Create a GitHub PR for a task that has been reviewer-approved.
    Returns a result dict with keys: ok, pr_url, pr_number, error, skipped.
    """
    es_id, task = get_task_doc(task_id)
    if not task:
        return {'ok': False, 'error': 'Task not found'}

    # Idempotency: don't create duplicate PRs
    if task.get('pr_url'):
        return {'ok': True, 'skipped': True, 'pr_url': task['pr_url'], 'pr_number': task.get('pr_number')}

    branch = task.get('branch')
    if not branch:
        return {'ok': False, 'error': 'No branch recorded on task — implementer must run first'}

    repo_id = task.get('repo')
    registry = load_projects_registry()
    proj = next((p for p in registry if p['id'] == repo_id), None)
    if not proj:
        return {'ok': False, 'error': f'Project "{repo_id}" not found in registry'}

    # AP-12: Explicit local-only guard — remote/indexed repos use GitHostClient.
    local_path = proj.get('path') or ''
    if not local_path or proj.get('clone_status') not in ('local',):
        return {'ok': False, 'error': 'PR creation via local git is only supported for locally-mounted repos. Remote repos use GitHostClient.'}
    repo_path = Path(local_path)
    if not (repo_path / '.git').exists():
        return {'ok': False, 'error': 'Repo path is not a git repository'}

    target_branch = await resolve_default_branch(
        repo_path,
        override=proj.get('gitflow', {}).get('defaultBranch'),
    )

    # Build PR title / body from task metadata
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
        f"{sha_line}\n\n"
        f"### Acceptance Criteria\n{ac_lines}\n\n"
        f"_Auto-generated by OpenClaw agent workflow._"
    )

    rc, out, err = await run_cmd_async("which", "gh")
    gh_path = out.strip() if rc == 0 else ""
    if not gh_path:
        return {'ok': False, 'error': '`gh` CLI not found — install GitHub CLI to enable PR creation'}

    try:
        rc, out, err = await run_cmd_async("gh", "pr", "create", "--base", target_branch, "--head", branch, "--title", title, "--body", body, cwd=str(repo_path), timeout=60)
        class _R:
            returncode = rc
            stdout = out
            stderr = err
        result = _R()
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'gh pr create timed out after 60s'}

    if result.returncode != 0:
        return {'ok': False, 'error': result.stderr.strip()[:500] or result.stdout.strip()[:500]}

    pr_url = result.stdout.strip()
    # Extract PR number from URL e.g. https://github.com/org/repo/pull/42
    pr_number = None
    url_parts = pr_url.rstrip('/').split('/')
    if url_parts and url_parts[-1].isdigit():
        pr_number = int(url_parts[-1])

    # Persist PR metadata to task doc
    if es_id:
        es_post(f'agent-task-records/_update/{es_id}', {
            'doc': {
                'pr_url': pr_url,
                'pr_number': pr_number,
                'pr_status': 'open',
                'target_branch': target_branch,
                'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                'last_update': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
        })

    return {'ok': True, 'pr_url': pr_url, 'pr_number': pr_number, 'target_branch': target_branch}


async def _git_task_context(task_id: str):
    """
    Shared helper: fetch task doc and resolve (task, repo_path, branch, target_branch).
    Returns (task, repo_path, branch, target_branch, error_dict).
    error_dict is non-None when something is missing.
    """
    _, task = get_task_doc(task_id)
    if not task:
        return None, None, None, None, {'error': 'Task not found', 'branch': None}
    branch = task.get('branch')
    if not branch:
        return task, None, None, None, {'error': 'No branch recorded on task yet', 'branch': None}
    repo_id = task.get('repo')
    registry = load_projects_registry()
    proj = next((p for p in registry if p['id'] == repo_id), None)
    if not proj:
        return task, None, branch, None, {'error': f'Project "{repo_id}" not found', 'branch': branch}
    # AP-12: Explicit local-only guard — no silent workspace fallback.
    local_path = proj.get('path') or ''
    if not local_path or proj.get('clone_status') not in ('local',):
        return task, None, branch, None, {'error': 'Git task context requires a locally-mounted repo (clone_status=local).', 'branch': branch}
    repo_path = Path(local_path)
    if not (repo_path / '.git').exists():
        return task, None, branch, None, {'error': 'Repo is not a git repository', 'branch': branch}
    target_branch = task.get('target_branch') or await resolve_default_branch(
        repo_path, override=proj.get('gitflow', {}).get('defaultBranch')
    )
    return task, repo_path, branch, target_branch, None


async def task_diff(task_id: str) -> dict:
    """Return unified diff of branch vs target branch (three-dot diff)."""
    task, repo_path, branch, target_branch, err = await _git_task_context(task_id)
    if err:
        return {**err, 'files': [], 'diff': '', 'truncated': False, 'target_branch': None}

    MAX_DIFF_LINES = 2000
    ref = f'origin/{target_branch}...{branch}'

    # Try fetch to ensure remote refs are current (best-effort, silent on failure)
    try:
        await run_cmd_async("git", "-C", str(repo_path), "fetch", "origin", "--quiet", timeout=10)
    except Exception as _e:
        logger.error("Ignored exception", exc_info=True)

    # --stat output to get per-file summary
    files = []
    try:
        stat_raw = subprocess.check_output(
            ['git', '-C', str(repo_path), 'diff', '--stat', '--stat-width=1000', ref],
            stderr=subprocess.DEVNULL, timeout=15,
        ).decode(errors='replace')
        for line in stat_raw.splitlines():
            # Format: " src/foo.py | 12 +++---"
            parts = line.strip().split('|')
            if len(parts) != 2:
                continue
            path_part = parts[0].strip()
            change_part = parts[1].strip()
            if not path_part or path_part.startswith('changed'):
                continue
            bars = change_part.split()
            plus_count = bars[1].count('+') if len(bars) > 1 else 0
            minus_count = bars[1].count('-') if len(bars) > 1 else 0
            files.append({
                'path': path_part,
                'insertions': plus_count,
                'deletions': minus_count,
                'status': 'modified',
            })
    except Exception:
        # Fall back to local diff if fetch/remote unavailable
        ref = f'{target_branch}...{branch}'

    # Full unified diff
    diff_text = ''
    truncated = False
    try:
        rc, out, err = await run_cmd_async("git", "-C", str(repo_path), "diff", ref, timeout=20)
        if rc != 0: raise Exception("git error")
        raw = out
        lines = raw.splitlines(keepends=True)
        if len(lines) > MAX_DIFF_LINES:
            diff_text = ''.join(lines[:MAX_DIFF_LINES])
            truncated = True
        else:
            diff_text = raw
    except Exception:
        diff_text = ''

    # If remote three-dot ref failed, fall back to local two-dot
    if not diff_text and not files:
        try:
            ref_local = f'{target_branch}..{branch}'
            raw = subprocess.check_output(
                ['git', '-C', str(repo_path), 'diff', ref_local],
                stderr=subprocess.DEVNULL, timeout=20,
            ).decode(errors='replace')
            lines = raw.splitlines(keepends=True)
            diff_text = ''.join(lines[:MAX_DIFF_LINES])
            truncated = len(lines) > MAX_DIFF_LINES
        except Exception as _e:
            logger.error("Ignored exception", exc_info=True)

    return {
        'branch': branch,
        'target_branch': target_branch,
        'files': files,
        'diff': diff_text,
        'truncated': truncated,
        'error': None,
    }


async def task_commits(task_id: str) -> dict:
    """Return commits on branch that are not on target branch."""
    task, repo_path, branch, target_branch, err = await _git_task_context(task_id)
    if err:
        return {**err, 'commits': [], 'target_branch': None}

    # Best-effort fetch
    try:
        await run_cmd_async("git", "-C", str(repo_path), "fetch", "origin", "--quiet", timeout=10)
    except Exception as _e:
        logger.error("Ignored exception", exc_info=True)

    commits = []
    # Try origin/target first, fall back to local target
    for ref_target in (f'origin/{target_branch}', target_branch):
        try:
            raw = subprocess.check_output(
                ['git', '-C', str(repo_path), 'log',
                 f'{ref_target}..{branch}',
                 '--pretty=format:%H|%an|%ai|%s',
                 '--max-count=50'],
                stderr=subprocess.DEVNULL, timeout=15,
            ).decode(errors='replace').strip()
            if raw:
                for line in raw.splitlines():
                    parts = line.split('|', 3)
                    if len(parts) == 4:
                        sha, author, date, message = parts
                        commits.append({
                            'sha': sha.strip(),
                            'author': author.strip(),
                            'date': date.strip(),
                            'message': message.strip(),
                        })
            break
        except Exception:
            continue

    return {
        'branch': branch,
        'target_branch': target_branch,
        'commits': commits,
        'error': None,
    }

