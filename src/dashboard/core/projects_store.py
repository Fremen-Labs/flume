import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

from utils.logger import get_logger
from core.elasticsearch import ES_URL, ctx

logger = get_logger(__name__)

PROJECTS_INDEX = "flume-projects"

def _ensure_gitflow_defaults(entry: dict) -> dict:
    """Backfill gitflow + concurrency config with defaults if missing."""
    if 'gitflow' not in entry:
        entry['gitflow'] = {
            'autoPrOnApprove': True,
            'defaultBranch': None,
            'integrationBranch': 'develop',
            'releaseBranch': 'main',
            'autoMergeIntegrationPr': True,
            'ensureIntegrationBranch': True,
        }
    else:
        gf = entry['gitflow']
        if 'autoPrOnApprove' not in gf:
            gf['autoPrOnApprove'] = True
        if 'defaultBranch' not in gf:
            gf['defaultBranch'] = None
        if 'integrationBranch' not in gf:
            gf['integrationBranch'] = 'develop'
        if 'releaseBranch' not in gf:
            gf['releaseBranch'] = 'main'
        if 'autoMergeIntegrationPr' not in gf:
            gf['autoMergeIntegrationPr'] = True
        if 'ensureIntegrationBranch' not in gf:
            gf['ensureIntegrationBranch'] = True
    try:
        from utils.concurrency_config import ensure_concurrency_defaults  # noqa: PLC0415
        ensure_concurrency_defaults(entry)
    except Exception:
        pass
    return entry

def _es_projects_request(path: str, body=None, method: str = "GET") -> dict:
    """Low-level ES request scoped to the projects index."""
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("ES_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    data = json.dumps(body).encode() if body is not None else None
    if data and method == "GET":
        method = "POST"
    req = urllib.request.Request(f"{ES_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise

def load_projects_registry() -> list:
    """Return all registered projects from Elasticsearch."""
    try:
        res = _es_projects_request(
            f"/{PROJECTS_INDEX}/_search",
            {"size": 500, "query": {"match_all": {}}, "sort": [{"created_at": {"order": "asc"}}]},
        )
        hits = res.get("hits", {}).get("hits", [])
        return [_ensure_gitflow_defaults(h["_source"]) for h in hits if h.get("_source")]
    except Exception as e:
        logger.warning({"event": "projects_load_error", "error": str(e)})
        return []

def save_projects_registry(registry: list):
    """
    Upsert the full list of projects into ES.
    Used for legacy callers that rewrite the entire list.
    """
    for entry in registry:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _es_projects_request(
                f"/{PROJECTS_INDEX}/_doc/{entry['id']}",
                entry,
                method="PUT",
            )
        except Exception as e:
            logger.warning({"event": "projects_save_error", "id": entry.get("id"), "error": str(e)})

def _upsert_project(entry: dict):
    """Upsert a single project document to ES.

    Uses ?refresh=wait_for so the document is immediately visible to searches
    (prevents the optimistic cache insert being overwritten by a stale poll
    before ES finishes indexing — fixes the 'project name disappears' bug).
    """
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    _es_projects_request(
        f"/{PROJECTS_INDEX}/_doc/{entry['id']}?refresh=wait_for",
        entry,
        method="PUT",
    )

def _update_project_registry_field(project_id: str, **fields) -> None:
    """Atomic field-level update on a single project document in ES.

    Uses ?refresh=wait_for so the clone_status change is visible to the
    /clone-status polling endpoint on the very next request — prevents the
    UI being stuck on 'cloning' when the clone has already failed or succeeded.
    """
    registry = load_projects_registry()
    for p in registry:
        if p.get('id') == project_id:
            p.update(fields)
            # Write back only the updated document with immediate consistency.
            p["updated_at"] = datetime.now(timezone.utc).isoformat()
            _es_projects_request(
                f"/{PROJECTS_INDEX}/_doc/{project_id}?refresh=wait_for",
                p,
                method="PUT",
            )
            return
    logger.warning(json.dumps({"event": "update_field_project_not_found", "project_id": project_id}))

def _delete_project_from_es(project_id: str):
    """Delete a project document from ES."""
    try:
        _es_projects_request(
            f"/{PROJECTS_INDEX}/_doc/{project_id}",
            method="DELETE",
        )
    except Exception as e:
        logger.warning({"event": "projects_delete_error", "id": project_id, "error": str(e)})
