import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

from utils.logger import get_logger
from core.elasticsearch import get_es_url, get_ssl_context, _get_auth_headers

logger = get_logger(__name__)

PROJECTS_INDEX = "flume-projects"

def _ensure_gitflow_defaults(entry: dict) -> dict:
    """Backfill gitflow + concurrency config with defaults if missing."""
    gf = entry.setdefault('gitflow', {})
    gf.setdefault('autoPrOnApprove', True)
    gf.setdefault('defaultBranch', None)
    gf.setdefault('integrationBranch', 'develop')
    gf.setdefault('releaseBranch', 'main')
    gf.setdefault('autoMergeIntegrationPr', True)
    gf.setdefault('ensureIntegrationBranch', True)
    try:
        from utils.concurrency_config import ensure_concurrency_defaults  # noqa: PLC0415
        ensure_concurrency_defaults(entry)
    except (ImportError, ValueError, TypeError):
        pass
    return entry

def _es_projects_request(path: str, body=None, method: str = "GET") -> dict:
    """Low-level ES request scoped to the projects index."""
    headers = {"Content-Type": "application/json"}
    headers.update(_get_auth_headers())
    data = json.dumps(body).encode() if body is not None else None
    if data and method == "GET":
        method = "POST"
    req = urllib.request.Request(f"{get_es_url()}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=get_ssl_context()) as resp:
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
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError) as e:
        logger.warning(
            "Projects load error",
            extra={"structured_data": {"event": "projects_load_error", "error": str(e)[:300]}},
            exc_info=True,
        )
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
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError) as e:
            logger.warning(
                "Projects save error",
                extra={"structured_data": {"event": "projects_save_error", "id": entry.get("id"), "error": str(e)[:300]}},
                exc_info=True,
            )

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

    Uses the ES _update API with doc_as_upsert to surgically patch only the
    changed fields — avoids the previous O(N) full-registry scan and rewrite.

    ?refresh=wait_for ensures the clone_status change is visible to the
    /clone-status polling endpoint on the very next request — prevents the
    UI being stuck on 'cloning' when the clone has already failed or succeeded.
    """
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _es_projects_request(
            f"/{PROJECTS_INDEX}/_update/{project_id}?refresh=wait_for",
            {"doc": fields, "doc_as_upsert": True},
            method="POST",
        )
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError) as e:
        logger.warning(
            "Project field update failed",
            extra={"structured_data": {"event": "update_field_project_failed", "project_id": project_id, "error": str(e)[:300]}},
            exc_info=True,
        )

def _delete_project_from_es(project_id: str):
    """Delete a project document from ES."""
    try:
        _es_projects_request(
            f"/{PROJECTS_INDEX}/_doc/{project_id}?refresh=wait_for",
            method="DELETE",
        )
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError) as e:
        logger.warning(
            "Project delete error",
            extra={"structured_data": {"event": "projects_delete_error", "id": project_id, "error": str(e)[:300]}},
            exc_info=True,
        )
