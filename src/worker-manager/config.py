"""Centralized configuration constants for the Flume worker-manager.

Eliminates duplicate ES_URL, NODE_ID, TASK_INDEX, and index constant
declarations that were previously independently defined in both
manager.py and worker_handlers.py. All modules should import from
here instead of redeclaring environment variables.
"""
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── Node Identity ────────────────────────────────────────────────────────────
NODE_ID: str = os.environ.get('HOSTNAME') or socket.gethostname() or "null-node"

# ── Workspace Root ───────────────────────────────────────────────────────────
# Resolves to flume/flume/src/ — the parent of worker-manager/
WORKSPACE_ROOT: Path = Path(__file__).resolve().parent.parent

# Ensure src/ is on sys.path so sibling packages (utils, dashboard) resolve.
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

WORKER_MANAGER_BASE: Path = WORKSPACE_ROOT / 'worker-manager'

# ── Elasticsearch ────────────────────────────────────────────────────────────
ES_URL: str = os.environ.get('ES_URL', 'http://elasticsearch:9200').rstrip('/')
ES_API_KEY: str = os.environ.get('ES_API_KEY', '')
ES_VERIFY_TLS: bool = os.environ.get('ES_VERIFY_TLS', 'false').lower() == 'true'

# ── Index Names ──────────────────────────────────────────────────────────────
TASK_INDEX: str = os.environ.get('ES_INDEX_TASKS', 'agent-task-records')
HANDOFF_INDEX: str = os.environ.get('ES_INDEX_HANDOFFS', 'agent-handoff-records')
FAILURE_INDEX: str = os.environ.get('ES_INDEX_FAILURES', 'agent-failure-records')
REVIEW_INDEX: str = os.environ.get('ES_INDEX_REVIEWS', 'agent-review-records')
PROVENANCE_INDEX: str = os.environ.get('ES_INDEX_PROVENANCE', 'agent-provenance-records')

# ── Polling & Backoff ────────────────────────────────────────────────────────
POLL_SECONDS: int = int(os.environ.get('WORKER_MANAGER_POLL_SECONDS', '2'))
BACKOFF_BASE_DELAY: float = float(os.environ.get('FLUME_BACKOFF_BASE_DELAY', '2.0'))
BACKOFF_MAX_DELAY: float = float(os.environ.get('FLUME_BACKOFF_MAX_DELAY', '30.0'))
BACKOFF_JITTER_FACTOR: float = float(os.environ.get('FLUME_BACKOFF_JITTER_FACTOR', '0.2'))

# ── Worker Pool ──────────────────────────────────────────────────────────────
POOL_SIZE: int = int(os.environ.get('FLUME_WORKER_POOL_SIZE', '24'))

# ── Agent Models (AP-8) ─────────────────────────────────────────────────────
# Kept as optional file-based fallback during rollout so existing
# agent_models.json files are still honoured until explicitly migrated.
from utils.workspace import resolve_safe_workspace  # noqa: E402
AGENT_MODELS_FILE: Path = resolve_safe_workspace() / 'worker-manager' / 'agent_models.json'
AGENT_MODELS_ES_ID: str = 'agent-models'  # document ID in flume-config index

# ── Role Ordering ────────────────────────────────────────────────────────────
ROLE_ORDER: list[str] = [
    'intake',
    'pm',
    'implementer',
    'tester',
    'reviewer',
    'memory-updater',
]


def now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()
