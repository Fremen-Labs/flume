"""Telemetry buffer and lifecycle event logging for the Flume worker-manager.

Extracted from manager.py (L168-230). Provides deferred bulk-flush
telemetry so the claim hot path never blocks on synchronous ES POSTs.

Usage:
    from es.telemetry import log_task_state_transition, log_telemetry_event, flush_telemetry
"""
import json

from config import now_iso
from es.client import es_request_raw
from utils.logger import get_logger

logger = get_logger('es.telemetry')

# ── Telemetry Buffer (Phase 1.3) ─────────────────────────────────────────
# Instead of firing synchronous ES POSTs per event on the claim hot path,
# buffer events in-memory and flush with a single _bulk call per cycle.
_TELEMETRY_BUFFER: list = []


def flush_telemetry() -> None:
    """Flush all buffered telemetry/lifecycle events to ES in a single _bulk call."""
    if not _TELEMETRY_BUFFER:
        return
    bulk_lines = []
    for entry in _TELEMETRY_BUFFER:
        idx = entry.pop('_index', 'flume-telemetry')
        bulk_lines.append(json.dumps({'index': {'_index': idx}}))
        bulk_lines.append(json.dumps(entry))
    _TELEMETRY_BUFFER.clear()
    if not bulk_lines:
        return
    try:
        es_request_raw('/_bulk?refresh=false', '\n'.join(bulk_lines) + '\n')
    except Exception as e:
        logger.error(f"telemetry bulk flush failed: {e}")


def log_task_state_transition(
    task_id: str,
    prev_status: str,
    new_status: str,
    role: str,
    worker_name: str,
    project: str = "",
) -> None:
    """Buffer a state transition event for deferred bulk flush."""
    _TELEMETRY_BUFFER.append({
        '_index': 'flume-task-events',
        'task_id': task_id,
        'previous_status': prev_status,
        'new_status': new_status,
        'role': role,
        'worker_name': worker_name,
        'owner': role,
        'project': project,
        'timestamp': now_iso(),
    })
    logger.debug(f"Lifecycle Event: Task {task_id} transitioned from {prev_status} to {new_status}")


def log_telemetry_event(
    worker_name: str,
    event_type: str,
    details: str,
    level: str = "INFO",
) -> None:
    """Buffer a telemetry event for deferred bulk flush."""
    ts = now_iso()
    _TELEMETRY_BUFFER.append({
        '_index': 'flume-telemetry',
        '@timestamp': ts,
        'timestamp': ts,
        'worker_name': worker_name,
        'event_type': event_type,
        'message': details,
        'level': level,
    })
