from datetime import datetime, timezone
from typing import Optional

from utils.exceptions import SAFE_EXCEPTIONS
from utils.logger import get_logger
from core.elasticsearch import es_search, es_post, async_es_search, async_es_post

logger = get_logger(__name__)

# ── Module Constants ─────────────────────────────────────────────────────────
_SESSIONS_INDEX = "agent-plan-sessions"
_ERR_TRUNCATE_LEN = 300


# ── Helpers ──────────────────────────────────────────────────────────────────

def _utcnow_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format with 'Z' suffix."""
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _iso_elapsed_seconds(started_at: Optional[str]) -> Optional[float]:
    """Return seconds elapsed since *started_at*, or None if unparseable."""
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
        return round((datetime.now(timezone.utc) - started).total_seconds(), 3)
    except SAFE_EXCEPTIONS:
        logger.debug(
            "ISO elapsed seconds parse failed",
            extra={"structured_data": {"event": "iso_elapsed_parse_failed", "started_at": started_at}},
            exc_info=True,
        )
        return None


# ── Synchronous API (legacy — kept for backward-compat callers) ──────────────

def load_session(session_id: str) -> Optional[dict]:
    """Load a planning session from ES by ID."""
    try:
        res = es_search(_SESSIONS_INDEX, {'size': 1, 'query': {'term': {'_id': session_id}}})
        hits = res.get('hits', {}).get('hits', [])
        if hits:
            return hits[0].get('_source')
    except SAFE_EXCEPTIONS as e:
        logger.error(
            "Error loading session from ES",
            extra={"structured_data": {"event": "session_load_failed", "session_id": session_id, "error": str(e)[:_ERR_TRUNCATE_LEN]}},
            exc_info=True,
        )
    return None


def save_session(session: dict) -> None:
    """Persist a planning session to ES."""
    try:
        session['updated_at'] = _utcnow_iso()
        es_post(f'{_SESSIONS_INDEX}/_doc/{session["id"]}?refresh=true', session)
    except SAFE_EXCEPTIONS as e:
        logger.error(
            "Error saving session to ES",
            extra={"structured_data": {"event": "session_save_failed", "session_id": session.get("id"), "error": str(e)[:_ERR_TRUNCATE_LEN]}},
            exc_info=True,
        )


# ── Async API ────────────────────────────────────────────────────────────────
# Non-blocking counterparts that use the centralized httpx client with
# exponential-backoff retry. Callers should migrate to these as their
# FastAPI endpoints are converted to async.

async def async_load_session(session_id: str) -> Optional[dict]:
    """Load a planning session from ES by ID (non-blocking)."""
    try:
        res = await async_es_search(_SESSIONS_INDEX, {'size': 1, 'query': {'term': {'_id': session_id}}})
        hits = res.get('hits', {}).get('hits', [])
        if hits:
            return hits[0].get('_source')
    except Exception as e:
        logger.error(
            "Async session load failed",
            extra={"structured_data": {"event": "async_session_load_failed", "session_id": session_id, "error": str(e)[:_ERR_TRUNCATE_LEN]}},
            exc_info=True,
        )
    return None


async def async_save_session(session: dict) -> None:
    """Persist a planning session to ES (non-blocking)."""
    try:
        session['updated_at'] = _utcnow_iso()
        await async_es_post(f'{_SESSIONS_INDEX}/_doc/{session["id"]}?refresh=true', session)
    except Exception as e:
        logger.error(
            "Async session save failed",
            extra={"structured_data": {"event": "async_session_save_failed", "session_id": session.get("id"), "error": str(e)[:_ERR_TRUNCATE_LEN]}},
            exc_info=True,
        )
