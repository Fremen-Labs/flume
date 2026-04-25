"""Workflow API router — Worker agent lifecycle management.

Extracted from server.py as part of the modular router decomposition.
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from utils.logger import get_logger
from core.tasks import load_workers
from core.elasticsearch import es_post
from core.sessions_store import _utcnow_iso

logger = get_logger(__name__)
router = APIRouter()


def _agents_status() -> dict:
    """Return live worker agent status from the server module.

    Delegates to server.agents_status() which inspects running PIDs.
    Imported lazily to avoid circular dependency with server.py.
    """
    from server import agents_status  # noqa: PLC0415
    return agents_status()


@router.get('/api/workflow/workers')
def api_workflow_workers():
    return {'workers': load_workers()}


@router.get('/api/workflow/agents/status')
def api_workflow_agents_status():
    try:
        return _agents_status()
    except Exception as e:
        logger.error({"event": "workflow_agents_status_failed", "error": str(e)[:200]})
        return JSONResponse(status_code=502, content={'error': str(e)[:200]})


@router.post('/api/workflow/agents/start')
def api_workflow_agents_start():
    try:
        es_post('agent-system-cluster/_doc/config', {'status': 'running', 'updated_at': _utcnow_iso()})
        return {'status': 'ok'}
    except Exception as e:
        logger.error({"event": "workflow_agents_start_failed", "error": str(e)[:200]})
        return JSONResponse(status_code=502, content={'error': str(e)[:200]})


@router.post('/api/workflow/agents/stop')
def api_workflow_agents_stop():
    try:
        es_post('agent-system-cluster/_doc/config', {'status': 'paused', 'updated_at': _utcnow_iso()})
        return {'status': 'ok'}
    except Exception as e:
        logger.error({"event": "workflow_agents_stop_failed", "error": str(e)[:200]})
        return JSONResponse(status_code=502, content={'error': str(e)[:200]})
