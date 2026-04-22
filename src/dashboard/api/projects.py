import os
import json
import uuid
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Query, BackgroundTasks
from fastapi.responses import JSONResponse

from utils.logger import get_logger
from utils.workspace import resolve_safe_workspace
from core.elasticsearch import es_search
from core.projects_store import load_projects_registry, _upsert_project, PROJECTS_INDEX, _es_projects_request

# Temporary circular imports from server.py (will be moved in Phase 2)
from server import _is_remote_url, _clone_and_setup_project, _deterministic_ast_ingest

logger = get_logger(__name__)
WORKSPACE_ROOT = resolve_safe_workspace()

router = APIRouter()

@router.post("/api/projects")
async def api_create_project(request: Request, payload: dict, background_tasks: BackgroundTasks):
    from utils.git_credentials import detect_repo_type, strip_credentials, _rewrite_url
    import ado_tokens_store
    import github_tokens_store

    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Project name is required."})

    repo_url = (payload.get("repoUrl") or "").strip()
    local_path_raw = (payload.get("localPath") or "").strip()

    new_id = f"proj-{uuid.uuid4().hex[:8]}"

    if _is_remote_url(repo_url):
        dest_path = Path(tempfile.mkdtemp(prefix=f"flume-reg-{new_id}-"))
        clone_status = 'cloning'
        resolved_path = None
    elif local_path_raw:
        dest_path = Path(local_path_raw).expanduser().resolve()
        clone_status = 'local'
        resolved_path = str(dest_path)
    else:
        dest_path = None
        clone_status = 'no_repo'
        resolved_path = None

    clone_url = strip_credentials(repo_url) if _is_remote_url(repo_url) else repo_url
    if _is_remote_url(repo_url):
        repo_type = detect_repo_type(repo_url)
        pat: str = ""
        if repo_type == "ado":
            try:
                raw_pat = ado_tokens_store.get_active_token_plain(WORKSPACE_ROOT)
                if raw_pat and "OPENBAO_DELEGATED" not in raw_pat:
                    pat = raw_pat
                elif raw_pat:
                    logger.warning(json.dumps({
                        "event": "ado_pat_placeholder_detected",
                        "project_id": new_id,
                        "hint": "OpenBao KV lookup returned placeholder. Falling back to ADO_PERSONAL_ACCESS_TOKEN env var.",
                    }))
            except Exception as _cred_err:
                logger.warning(json.dumps({
                    "event": "ado_pat_fetch_error",
                    "project_id": new_id,
                    "error": str(_cred_err)[:200],
                }))
            _DELEGATED = "OPENBAO_DELEGATED"
            if not pat:
                try:
                    from llm_settings import _openbao_get_all
                    _bao_vals = _openbao_get_all(WORKSPACE_ROOT)
                    _bao_ado = str(_bao_vals.get("ADO_TOKEN") or "").strip()
                    if _bao_ado and _DELEGATED not in _bao_ado:
                        pat = _bao_ado
                        logger.info(json.dumps({
                            "event": "ado_pat_from_openbao_direct",
                            "project_id": new_id,
                            "hint": "PAT sourced from OpenBao ADO_TOKEN key (flume start provisioning).",
                        }))
                except Exception:
                    pass
            if not pat:
                _env_pat = (
                    os.environ.get("ADO_PERSONAL_ACCESS_TOKEN", "").strip()
                    or os.environ.get("ADO_TOKEN", "").strip()
                )
                if _env_pat and _DELEGATED not in _env_pat:
                    pat = _env_pat
                    logger.info(json.dumps({
                        "event": "ado_pat_from_env",
                        "project_id": new_id,
                        "hint": "PAT sourced from ADO_PERSONAL_ACCESS_TOKEN env var (OpenBao fallback).",
                    }))
                elif _env_pat:
                    logger.warning(json.dumps({
                        "event": "ado_pat_sentinel_in_env",
                        "project_id": new_id,
                        "hint": "ADO_TOKEN env var contains OPENBAO_DELEGATED sentinel — OpenBao not yet seeded.",
                    }))
        elif repo_type == "github":
            try:
                raw_pat = github_tokens_store.get_active_token_plain(WORKSPACE_ROOT)
                if raw_pat and "OPENBAO_DELEGATED" not in raw_pat:
                    pat = raw_pat
                elif raw_pat:
                    logger.warning(json.dumps({
                        "event": "gh_pat_placeholder_detected",
                        "project_id": new_id,
                        "hint": "OpenBao KV lookup returned placeholder. Falling back to GITHUB_TOKEN env var.",
                    }))
            except Exception as _cred_err:
                logger.warning(json.dumps({
                    "event": "gh_pat_fetch_error",
                    "project_id": new_id,
                    "error": str(_cred_err)[:200],
                }))
            _DELEGATED = "OPENBAO_DELEGATED"
            if not pat:
                _env_pat = os.environ.get("GITHUB_TOKEN", "").strip()
                if _env_pat and _DELEGATED not in _env_pat:
                    pat = _env_pat
                    logger.info(json.dumps({
                        "event": "gh_pat_from_env",
                        "project_id": new_id,
                        "hint": "PAT sourced from GITHUB_TOKEN env var (OpenBao fallback).",
                    }))
                elif _env_pat:
                    logger.warning(json.dumps({
                        "event": "gh_pat_sentinel_in_env",
                        "project_id": new_id,
                        "hint": "GITHUB_TOKEN env var contains OPENBAO_DELEGATED sentinel — OpenBao not yet seeded.",
                    }))
        if pat:
            clone_url = _rewrite_url(clone_url, pat)
            logger.info(json.dumps({
                "event": "project_clone_credentials_embedded",
                "project_id": new_id,
                "repo_type": repo_type,
            }))
        else:
            logger.warning(json.dumps({
                "event": "project_clone_no_credentials",
                "project_id": new_id,
                "repo_type": repo_type,
                "hint": "Add credentials via Settings → Repositories before cloning private repos.",
            }))

    entry = {
        "id": new_id,
        "name": name,
        "repoUrl": repo_url,
        "path": resolved_path,
        "clone_status": clone_status,
        "clone_error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "gitflow": {
            "autoPrOnApprove": True,
            "defaultBranch": None,
            "integrationBranch": "develop",
            "releaseBranch": "main",
            "autoMergeIntegrationPr": True,
            "ensureIntegrationBranch": True,
        },
        "concurrency": {
            "maxRunningPerRepo": 2,
            "maxReadyPerRepo": 4,
            "storyParallelism": 1,
            "serializeIntegrationMerge": True,
        },
    }

    _upsert_project(entry)

    http_client = request.app.state.http_client

    if _is_remote_url(repo_url):
        background_tasks.add_task(
            _clone_and_setup_project,
            http_client, new_id, name, clone_url, dest_path,
        )
    elif local_path_raw and dest_path.is_dir():
        background_tasks.add_task(
            _deterministic_ast_ingest, http_client, resolved_path, new_id, name,
        )

    return {"success": True, "projectId": new_id, "project": entry, "message": "Project created."}

@router.get("/api/projects/{project_id}/clone-status")
def api_project_clone_status(project_id: str):
    registry = load_projects_registry()
    proj = next((p for p in registry if p.get('id') == project_id), None)
    if not proj:
        return JSONResponse(status_code=404, content={'error': f"Project '{project_id}' not found"})
    return {
        'projectId': project_id,
        'clone_status': proj.get('clone_status', 'unknown'),
        'clone_error': proj.get('clone_error'),
        'path': proj.get('path'),
        'is_git': (Path(proj.get('path', '')) / '.git').exists() if proj.get('path') else False,
    }

def _map_task_hit_for_api(h: dict) -> dict:
    s = h.get('_source', {})
    res = {'_id': h.get('_id'), **s}
    thoughts = s.get('execution_thoughts', [])
    res['execution_thoughts_count'] = len(thoughts) if isinstance(thoughts, list) else 0
    if 'execution_thoughts' in res:
        del res['execution_thoughts']
    return res

@router.get("/api/projects/{project_id}/tasks")
def api_project_tasks(
    project_id: str,
    archived: str = Query(
        'exclude',
        description='exclude=active only (default); include=all statuses; only=archived items only',
    ),
):
    registry = load_projects_registry()
    if not any(p.get('id') == project_id for p in registry):
        return JSONResponse(status_code=404, content={'error': f"Project '{project_id}' not found"})
    mode = (archived or 'exclude').strip().lower()
    if mode not in ('exclude', 'include', 'only'):
        return JSONResponse(
            status_code=400,
            content={'error': 'archived must be one of: exclude, include, only'},
        )
    try:
        if mode == 'include':
            query = {'bool': {'must': [{'term': {'repo': project_id}}]}}
        elif mode == 'only':
            query = {
                'bool': {
                    'must': [
                        {'term': {'repo': project_id}},
                        {'term': {'status': 'archived'}},
                    ],
                },
            }
        else:
            query = {
                'bool': {
                    'must': [{'term': {'repo': project_id}}],
                    'must_not': [{'term': {'status': 'archived'}}],
                },
            }
        res = es_search('agent-task-records', {
            'size': 5000,
            'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': query,
        })
    except Exception as e:
        logger.warning(json.dumps({'event': 'project_tasks_query_failed', 'project_id': project_id, 'error': str(e)[:300]}))
        return JSONResponse(status_code=500, content={'error': str(e)[:400]})
    hits = res.get('hits', {}).get('hits', [])
    return {
        'projectId': project_id,
        'archived': mode,
        'tasks': [_map_task_hit_for_api(h) for h in hits],
    }

def _project_task_ids_for_repo(project_id: str) -> list[str]:
    try:
        res = es_search('agent-task-records', {
            'size': 5000,
            '_source': ['id'],
            'query': {'term': {'repo': project_id}},
        })
    except Exception:
        return []
    out = []
    for h in res.get('hits', {}).get('hits', []) or []:
        tid = (h.get('_source') or {}).get('id')
        if tid:
            out.append(str(tid))
    return out

@router.get("/api/projects/{project_id}/activity")
def api_project_activity(project_id: str, limit: int = Query(250, ge=10, le=1000)):
    registry = load_projects_registry()
    if not any(p.get('id') == project_id for p in registry):
        return JSONResponse(status_code=404, content={'error': f"Project '{project_id}' not found"})
    task_ids = _project_task_ids_for_repo(project_id)
    if not task_ids:
        return {'projectId': project_id, 'events': [], 'taskCount': 0}

    q_terms = {'terms': {'task_id': task_ids}}
    events: list[dict] = []

    def add_batch(index: str, hits: list, typ: str, ts_key: str, summary_fn, detail_fn):
        for h in hits:
            src = h.get('_source') or {}
            tid = src.get('task_id') or src.get('taskId')
            ts = src.get(ts_key) or src.get('created_at') or src.get('updated_at')
            events.append({
                'type': typ,
                'task_id': tid,
                'timestamp': ts,
                'summary': summary_fn(src),
                'details': detail_fn(src),
            })

    try:
        hand = es_search('agent-handoff-records', {
            'size': min(limit, 500),
            'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': q_terms,
        }).get('hits', {}).get('hits', [])
        add_batch(
            'agent-handoff-records', hand, 'handoff', 'created_at',
            lambda s: f"{s.get('from_role', '?')} → {s.get('to_role', '?')}",
            lambda s: (s.get('reason') or '')[:500],
        )
        rev = es_search('agent-review-records', {
            'size': min(limit, 500),
            'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': q_terms,
        }).get('hits', {}).get('hits', [])
        add_batch(
            'agent-review-records', rev, 'review', 'created_at',
            lambda s: f"Review: {s.get('verdict', '?')}",
            lambda s: (s.get('summary') or '')[:500],
        )
        fail = es_search('agent-failure-records', {
            'size': min(limit, 500),
            'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': q_terms,
        }).get('hits', {}).get('hits', [])
        add_batch(
            'agent-failure-records', fail, 'failure', 'updated_at',
            lambda s: str(s.get('error_class') or 'failure'),
            lambda s: (s.get('summary') or '')[:500],
        )
        prov = es_search('agent-provenance-records', {
            'size': min(limit, 500),
            'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': q_terms,
        }).get('hits', {}).get('hits', [])
        add_batch(
            'agent-provenance-records', prov, 'provenance', 'created_at',
            lambda s: f"Provenance ({s.get('agent_role', 'agent')})",
            lambda s: (s.get('review_verdict') or '')[:300],
        )
    except Exception as e:
        logger.warning(json.dumps({'event': 'project_activity_query_failed', 'project_id': project_id, 'error': str(e)[:300]}))

    try:
        te = es_search('flume-task-events', {
            'size': min(limit, 500),
            'sort': [{'timestamp': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': q_terms,
        }).get('hits', {}).get('hits', [])
        for h in te:
            src = h.get('_source') or {}
            tid = src.get('task_id')
            ts = src.get('timestamp')
            et = src.get('event_type') or 'event'
            det = src.get('details') if isinstance(src.get('details'), dict) else {}
            summary = det.get('status') or det.get('pr_status') or et
            events.append({
                'type': 'task_event',
                'task_id': tid,
                'timestamp': ts,
                'summary': str(summary)[:200],
                'details': json.dumps(det)[:500] if det else '',
            })
    except Exception:
        pass

    events = [e for e in events if e.get('timestamp')]
    events.sort(key=lambda e: e.get('timestamp') or '', reverse=True)
    events = events[:limit]
    return {'projectId': project_id, 'taskCount': len(task_ids), 'events': events}

@router.post("/api/projects/{project_id}/delete")
def api_delete_project(project_id: str):
    registry = load_projects_registry()
    project_found = any(p.get("id") == project_id for p in registry)

    if not project_found:
        return JSONResponse(
            status_code=404,
            content={"error": f"Project '{project_id}' not found"},
        )

    try:
        _es_projects_request(
            f"/{PROJECTS_INDEX}/_doc/{project_id}?refresh=wait_for",
            method="DELETE",
        )
        logger.info(json.dumps({
            "event": "project_deleted",
            "project_id": project_id,
        }))
    except Exception as exc:
        logger.warning(json.dumps({
            "event": "project_delete_es_error",
            "project_id": project_id,
            "error": str(exc)[:200],
        }))

    project_doc = next((p for p in registry if p.get("id") == project_id), {})
    local_path = project_doc.get("path")
    clone_status = project_doc.get("clone_status", "")
    if local_path and clone_status == "local":
        dest_path = Path(local_path)
        logger.info(json.dumps({
            "event": "project_local_path_note",
            "project_id": project_id,
            "path": str(dest_path),
            "note": "Local path not auto-deleted (user-managed). Remove manually if desired.",
        }))

    return {"success": True, "projectId": project_id, "message": "Project removed."}
