import urllib.error
from utils.exceptions import SAFE_EXCEPTIONS
from datetime import datetime, timezone
from typing import Optional

from utils.logger import get_logger
from core.elasticsearch import es_search, es_post

logger = get_logger(__name__)

def load_session(session_id: str) -> Optional[dict]:
    try:
        res = es_search('agent-plan-sessions', {'size': 1, 'query': {'term': {'_id': session_id}}})
        hits = res.get('hits', {}).get('hits', [])
        if hits:
            return hits[0].get('_source')
    except SAFE_EXCEPTIONS as e:
        logger.error("Error loading session from ES", extra={"structured_data": {"session_id": session_id, "error": str(e)}})
    return None


def save_session(session: dict) -> None:
    try:
        session['updated_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        es_post(f'agent-plan-sessions/_doc/{session["id"]}?refresh=true', session)
    except SAFE_EXCEPTIONS as e:
        logger.error("Error saving session to ES", extra={"structured_data": {"error": str(e)}})


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _iso_elapsed_seconds(started_at: Optional[str]) -> Optional[float]:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
        return round((datetime.now(timezone.utc) - started).total_seconds(), 3)
    except SAFE_EXCEPTIONS:
        return None
