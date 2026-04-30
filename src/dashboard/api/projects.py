"""Project lifecycle router — creation, listing, status, and deletion."""
import os
import uuid
import tempfile
import httpx
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Query, BackgroundTasks
from api.models import ProjectCreateRequest
from fastapi.responses import JSONResponse

import functools
from utils.logger import get_logger
from utils.exceptions import SAFE_EXCEPTIONS
from utils.workspace import resolve_safe_workspace
from core.elasticsearch import async_es_search
from core.projects_store import load_projects_registry, _upsert_project, PROJECTS_INDEX, _es_projects_request
from utils.url_helpers import is_remote_url


logger = get_logger(__name__)

@functools.lru_cache(maxsize=1)
def _get_workspace_root():
    return resolve_safe_workspace()

router = APIRouter()

@router.post("/api/projects")
async def api_create_project(request: Request, payload: ProjectCreateRequest, background_tasks: BackgroundTasks):
    from core.project_lifecycle import _clone_and_setup_project, _deterministic_ast_ingest

    name = (payload.name or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Project name is required."})

    repo_url = (payload.repoUrl or "").strip()
    local_path_raw = (payload.localPath or "").strip()

    new_id = f"proj-{uuid.uuid4().hex[:8]}"

    if is_remote_url(repo_url):
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

    clone_url = repo_url
    if is_remote_url(repo_url):
        from utils.git_credentials import embed_credentials, detect_repo_type, strip_credentials
        repo_type = detect_repo_type(repo_url)
        embedded_url = embed_credentials(repo_url, repo_type)
        if embedded_url != repo_url:
            clone_url = embedded_url
            logger.info(
                "Project clone credentials embedded",
                extra={"structured_data": {"event": "project_clone_credentials_embedded", "project_id": new_id, "repo_type": repo_type}}
            )
        else:
            clone_url = strip_credentials(repo_url)
            logger.warning(
                "Project clone no credentials",
                extra={
                    "structured_data": {
                        "event": "project_clone_no_credentials",
                        "project_id": new_id,
                        "repo_type": repo_type,
                        "hint": "Add credentials via Settings → Repositories before cloning private repos."
                    }
                }
            )

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

    if is_remote_url(repo_url):
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
async def api_project_tasks(
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
        res = await async_es_search('agent-task-records', {
            'size': 5000,
            'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': query,
        })
    except SAFE_EXCEPTIONS as e:
        logger.warning(
            "Project tasks query failed",
            extra={"structured_data": {"event": "project_tasks_query_failed", "project_id": project_id, "error": str(e)[:300]}}
        )
        return JSONResponse(status_code=500, content={'error': str(e)[:400]})
    hits = res.get('hits', {}).get('hits', [])
    return {
        'projectId': project_id,
        'archived': mode,
        'tasks': [_map_task_hit_for_api(h) for h in hits],
    }

async def _project_task_ids_for_repo(project_id: str) -> list[str]:
    try:
        res = await async_es_search('agent-task-records', {
            'size': 5000,
            '_source': ['id'],
            'query': {'term': {'repo': project_id}},
        })
    except SAFE_EXCEPTIONS:
        return []
    out = []
    for h in res.get('hits', {}).get('hits', []) or []:
        tid = (h.get('_source') or {}).get('id')
        if tid:
            out.append(str(tid))
    return out


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
        logger.info(
            "Project deleted",
            extra={"structured_data": {"event": "project_deleted", "project_id": project_id}}
        )
    except SAFE_EXCEPTIONS as exc:
        logger.warning(
            "Project deletion ES error",
            extra={"structured_data": {"event": "project_delete_es_error", "project_id": project_id, "error": str(exc)[:200]}}
        )

    project_doc = next((p for p in registry if p.get("id") == project_id), {})
    local_path = project_doc.get("path")
    clone_status = project_doc.get("clone_status", "")
    if local_path and clone_status == "local":
        dest_path = Path(local_path)
        logger.info(
            "Project local path note",
            extra={
                "structured_data": {
                    "event": "project_local_path_note",
                    "project_id": project_id,
                    "path": str(dest_path),
                    "note": "Local path not auto-deleted (user-managed). Remove manually if desired."
                }
            }
        )

    return {"success": True, "projectId": project_id, "message": "Project removed."}
