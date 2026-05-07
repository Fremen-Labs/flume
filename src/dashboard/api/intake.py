"""Intake router — AI planning session lifecycle (create → refine → commit)."""
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from utils.logger import get_logger
from utils.exceptions import SAFE_EXCEPTIONS
from core.sessions_store import load_session, save_session
from api.models import IntakeSessionRequest, IntakeMessageRequest, IntakeCommitRequest

from core.planning import (
    create_planning_session,
    refine_session,
    commit_plan,
    _count_plan_tasks,
)
from api.models import PlanResponse

from utils.session_helpers import _session_payload_for_client

logger = get_logger(__name__)
router = APIRouter()

@router.post('/api/intake/session')
async def api_intake_start_session(payload: IntakeSessionRequest):
    repo = payload.repo.strip()
    prompt = payload.prompt.strip()
    if not repo:
        return JSONResponse(status_code=400, content={'error': 'repo is required'})
    if not prompt:
        return JSONResponse(status_code=400, content={'error': 'prompt is required'})
    try:
        session = await create_planning_session(repo, prompt)
        return _session_payload_for_client(session)
    except SAFE_EXCEPTIONS as e:
        logger.error(
            "Intake session creation failed",
            extra={"structured_data": {"event": "intake_session_create_failed", "error": str(e)[:400]}},
            exc_info=True,
        )
        return JSONResponse(status_code=500, content={'error': str(e)[:400]})

@router.get('/api/intake/session/{session_id}')
def api_intake_get_session(session_id: str):
    session = load_session(session_id)
    if not session:
        return JSONResponse(status_code=404, content={'error': 'session not found'})
    return _session_payload_for_client(session)

@router.post('/api/intake/session/{session_id}/message')
async def api_intake_message(session_id: str, payload: IntakeMessageRequest):
    text = payload.text.strip()
    if not text:
        return JSONResponse(status_code=400, content={'error': 'text is required'})
    plan = payload.plan if isinstance(payload.plan, dict) else None
    try:
        session = await refine_session(session_id, text, plan)
        if not session:
            return JSONResponse(status_code=404, content={'error': 'session not found'})
        return _session_payload_for_client(session)
    except SAFE_EXCEPTIONS as e:
        logger.error(
            "Intake session refinement failed",
            extra={"structured_data": {"event": "intake_session_refine_failed", "error": str(e)[:400]}},
            exc_info=True,
        )
        return JSONResponse(status_code=500, content={'error': str(e)[:400]})

@router.post('/api/intake/session/{session_id}/commit')
async def api_intake_commit(session_id: str, payload: IntakeCommitRequest):
    session = load_session(session_id)
    if not session:
        return JSONResponse(status_code=404, content={'error': 'session not found'})

    plan = payload.plan if isinstance(payload.plan, dict) else session.get('draftPlan')
    if not plan or not (plan.get('epics') or []):
        return JSONResponse(status_code=400, content={'error': 'plan is empty'})

    repo = session.get('repo') or (payload.repo or '').strip()
    if not repo:
        return JSONResponse(status_code=400, content={'error': 'repo is required'})

    try:
        docs, _results = await commit_plan(repo, plan)
        session['status'] = 'committed'
        session['draftPlan'] = plan
        session['committed_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        session['committedDocs'] = [d.get('id') for d in docs]
        save_session(session)
        return {
            'ok': True,
            'count': _count_plan_tasks(PlanResponse.model_validate(plan)),
            'created': len(docs),
            'taskIds': [d.get('id') for d in docs if d.get('item_type') == 'task'],
        }
    except SAFE_EXCEPTIONS as e:
        logger.error(
            "Intake plan commit failed",
            extra={"structured_data": {"event": "intake_plan_commit_failed", "error": str(e)[:400]}},
            exc_info=True,
        )
        return JSONResponse(status_code=500, content={'error': str(e)[:400]})
    except Exception as e:
        # Defense-in-depth: Pydantic ValidationError and other unexpected
        # exceptions must still return valid JSON so the frontend can parse
        # the response — never a raw HTML 500 page.
        logger.error(
            "Intake plan commit unexpected error",
            extra={"structured_data": {"event": "intake_plan_commit_unexpected", "error": str(e)[:400]}},
            exc_info=True,
        )
        return JSONResponse(status_code=500, content={'error': f'Plan commit failed: {str(e)[:400]}'})
