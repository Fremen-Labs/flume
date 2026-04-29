"""Process manager — Worker agent lifecycle orchestration.

Decoupled from server.py (Phase 3) to enforce Single Responsibility.
Contains all OS-level process management: starting, stopping, and
restarting worker manager and handler processes via subprocess.Popen.
"""
import os
import sys
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

from utils.logger import get_logger
from utils.exceptions import SAFE_EXCEPTIONS
from core.elasticsearch import es_search, es_post
from utils.workspace import WORKSPACE_ROOT

logger = get_logger(__name__)

_SRC_ROOT = Path(__file__).resolve().parent.parent.parent  # src/


def _find_worker_pids() -> dict:
    # Deprecated: Handled asynchronously heavily via agent-system-workers heartbeat schema
    return {'manager': [], 'handlers': []}


def agents_status() -> dict:
    """Fetch live agent cluster status from Elasticsearch heartbeats."""
    try:
        # 1. Fetch Admin Control Status
        clust = es_search('agent-system-cluster', {'size': 1, 'query': {'term': {'_id': 'config'}}})
        c_hits = clust.get('hits', {}).get('hits', [])
        status = 'running'
        if c_hits:
            status = c_hits[0].get('_source', {}).get('status', 'running')

        # 2. Fetch Aggregated Node Heartbeats
        w_res = es_search('agent-system-workers', {'size': 100, 'sort': [{'updated_at': {'order': 'desc'}}]})
        w_hits = w_res.get('hits', {}).get('hits', [])
        now = datetime.now(timezone.utc)
        active_nodes = 0
        
        for h in w_hits:
            doc = h.get('_source', {})
            hb_str = doc.get('updated_at')
            if hb_str:
                try:
                    hb = datetime.fromisoformat(hb_str.replace('Z', '+00:00'))
                    if (now - hb).total_seconds() <= 30:
                        active_nodes += 1
                except SAFE_EXCEPTIONS:
                    logger.debug("agents_status: heartbeat timestamp parse failed", exc_info=True)

        return {
            'running': active_nodes > 0 and status != 'paused',
            'manager_running': active_nodes > 0,
            'handlers_running': active_nodes > 0,
            'manager_pids': [],
            'handler_pids': [],
            'cluster_status': status
        }
    except SAFE_EXCEPTIONS as e:
        logger.error("Error fetching agent status", extra={"structured_data": {"error": str(e)}})
        return {'running': False, 'error': str(e)}


def _requeue_running_tasks():
    """After stopping workers, reset tasks stuck in 'running' back to queued."""
    try:
        hits = es_search('agent-task-records', {
            'size': 200,
            'query': {'term': {'status': 'running'}},
        }).get('hits', {}).get('hits', [])
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        requeued = 0
        for h in hits:
            es_id = h.get('_id')
            src = h.get('_source', {})
            role = src.get('assigned_agent_role') or src.get('owner') or ''
            if role in ('tester', 'reviewer'):
                new_status = 'review'
            elif role == 'pm':
                new_status = 'planned'
            else:
                new_status = 'ready'
            es_post(f'agent-task-records/_update/{es_id}', {
                'doc': {
                    'status': new_status,
                    'updated_at': now,
                    'last_update': now,
                    'active_worker': None,
                }
            })
            requeued += 1
        logger.info("_requeue_running_tasks: requeued %d tasks", requeued)
        return requeued
    except SAFE_EXCEPTIONS:
        logger.error("_requeue_running_tasks: ES update failed", exc_info=True)
        return 0


def agents_stop() -> dict:
    """Kill worker processes and re-queue any stuck running tasks."""
    pids = _find_worker_pids()
    killed = []
    for group in ('manager', 'handlers'):
        for pid in (pids.get(group) or []):
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except SAFE_EXCEPTIONS:
                logger.warning("agents_stop: SIGTERM failed for pid (may already be dead)", exc_info=True)
    requeued = _requeue_running_tasks()
    logger.info("agents_stop: killed=%s, requeued=%d", killed, requeued)
    return {'ok': True, 'killed_pids': killed, 'requeued_tasks': requeued}


def agents_start() -> dict:
    """Start manager and worker_handlers if not already running."""
    pids = _find_worker_pids()
    started = []

    env = dict(os.environ)

    manager_err = subprocess.DEVNULL
    handlers_err = subprocess.DEVNULL

    python_bin = sys.executable

    WORKER_MANAGER_SCRIPT = _SRC_ROOT / 'worker-manager' / 'manager.py'
    WORKER_HANDLERS_SCRIPT = _SRC_ROOT / 'worker-manager' / 'worker_handlers.py'

    if not pids['manager']:
        proc = subprocess.Popen(
            [python_bin, str(WORKER_MANAGER_SCRIPT)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=manager_err,
            start_new_session=True,
        )
        started.append({'role': 'manager', 'pid': proc.pid})

    if not pids['handlers']:
        proc = subprocess.Popen(
            [python_bin, str(WORKER_HANDLERS_SCRIPT)],
            env=env,
            cwd=str(_SRC_ROOT / 'worker-manager'),
            stdout=subprocess.DEVNULL,
            stderr=handlers_err,
            start_new_session=True,
        )
        started.append({'role': 'handlers', 'pid': proc.pid})

    if started:
        logger.info("agents_start: launched %s", started)
    return {'ok': True, 'started': started, 'already_running': not started}


def _resolve_flume_cli() -> Optional[Path]:
    """Path to the `flume` driver script at repo or package root, or None."""
    w = WORKSPACE_ROOT.resolve()
    for base in (w, w.parent):
        candidate = base / 'flume'
        if candidate.is_file():
            return candidate
    return None


def restart_flume_services() -> dict:
    """Schedule `./flume restart --all` or fall back to worker-only restart."""
    flume_sh = _resolve_flume_cli()
    if flume_sh is not None:
        root = flume_sh.parent.resolve()
        script = flume_sh.name
        inner = (
            f'cd {shlex.quote(str(root))} && sleep 0.5 && exec bash {shlex.quote(script)} restart --all'
        )
        try:
            subprocess.Popen(
                ['bash', '-c', inner],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            logger.info("restart_flume_services: scheduled full restart via flume CLI")
            return {
                'ok': True,
                'mode': 'flume',
                'message': 'Restart scheduled. You may lose connection briefly; refresh if the page stops responding.',
            }
        except SAFE_EXCEPTIONS:
            logger.warning("restart_flume_services: flume CLI restart failed, falling back to workers_only", exc_info=True)
    try:
        agents_stop()
        started = agents_start()
        return {
            'ok': True,
            'mode': 'workers_only',
            'message': 'Worker processes restarted. Restart the dashboard manually if configuration still looks stale.',
            'workers': started,
        }
    except SAFE_EXCEPTIONS as e:
        logger.error("restart_flume_services: fallback restart failed", extra={"structured_data": {"error": str(e)[:400]}})
        return {'ok': False, 'error': str(e)[:400]}


def maybe_auto_start_workers():
    """Auto-start workers on dashboard boot. Disable with FLUME_AUTO_START_WORKERS=0."""
    raw = os.environ.get('FLUME_AUTO_START_WORKERS', '1').strip().lower()
    if raw in ('0', 'false', 'no', 'off'):
        return
    try:
        result = agents_start()
        started = result.get('started') or []
        if started:
            logger.info('Flume: auto-started workers: %s', started)
        elif result.get('already_running'):
            logger.info('Flume: workers already running (skipped auto-start).')
    except SAFE_EXCEPTIONS as e:
        logger.warning('Flume: could not auto-start workers: %s', e)
