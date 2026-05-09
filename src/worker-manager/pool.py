"""Worker process pool lifecycle management for the Flume worker-manager.

Extracted from manager.py (L50-82, L1619-1722). Provides:
  - Lazy-initialized ProcessPoolExecutor with forkserver/spawn fallback
  - Signal-safe shutdown flag and handler
  - Graceful pool drainage, orphan task requeue, and temp dir cleanup

Usage:
    from pool import get_worker_pool, shutdown_requested, shutdown_pool_signal, perform_graceful_shutdown
"""
import concurrent.futures
import multiprocessing
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Optional

from config import NODE_ID, POOL_SIZE, TASK_INDEX, now_iso
from es.client import es_request
from utils.logger import get_logger

logger = get_logger('pool')

# ── Pool Singleton ───────────────────────────────────────────────────────────
_WORKER_POOL: Optional[ProcessPoolExecutor] = None

# Track in-flight futures to avoid double-dispatching a worker
# and to harvest results/errors.
active_futures: dict[str, concurrent.futures.Future] = {}  # worker_name -> Future

# ── Shutdown Flag ────────────────────────────────────────────────────────────
shutdown_requested: bool = False


def get_worker_pool() -> ProcessPoolExecutor:
    """Lazy-init the process pool. Only the main manager process creates it.

    This prevents a fork bomb: if child processes re-import this module,
    they won't create nested pools because get_worker_pool() is only
    called from sync_worker_processes() inside cycle().
    """
    global _WORKER_POOL
    if _WORKER_POOL is None:
        if multiprocessing.current_process().name != 'MainProcess':
            raise RuntimeError("Worker pool can only be created by the MainProcess")
        try:
            multiprocessing.set_start_method('forkserver', force=True)
        except (ValueError, RuntimeError):
            try:
                multiprocessing.set_start_method('spawn', force=True)
            except RuntimeError:
                pass  # already set — safe to continue
        _WORKER_POOL = ProcessPoolExecutor(max_workers=POOL_SIZE)
    return _WORKER_POOL


def shutdown_pool_signal(signum=None, frame=None) -> None:
    """Minimal signal handler — sets the shutdown flag only."""
    global shutdown_requested
    if not shutdown_requested:
        shutdown_requested = True
        logger.info("manager: graceful shutdown requested via signal (waiting for cycle to end)...")


def _release_orphaned_tasks() -> None:
    """Fetch tasks currently assigned to this node and reset them to ready via FSM."""
    try:
        body = {
            "size": 100,
            "_source": ["status", "active_worker"],
            "query": {
                "bool": {
                    "must": [
                        {"term": {"status": "running"}},
                        {"wildcard": {"active_worker": f"*-{NODE_ID}-*"}}
                    ]
                }
            }
        }
        res = es_request(f'/{TASK_INDEX}/_search', body, method='GET')
        hits = res.get('hits', {}).get('hits', [])

        if not hits:
            return

        from lifecycle.state_machine import TaskStateMachine

        for h in hits:
            task_id = h['_id']
            src = h.get('_source', {})
            current_status = src.get('status', 'running')

            try:
                TaskStateMachine.validate_transition(current_status, 'ready')
            except Exception as e:
                logger.warning(f"pool-shutdown: invalid transition for {task_id}: {e}")
                continue

            update_body = {
                'doc': {
                    'status': 'ready',
                    'active_worker': None,
                    'queue_state': 'interrupted',
                    'updated_at': now_iso(),
                    'agent_log': [{'note': 'Worker node shutting down; task interrupted and re-queued.', 'ts': now_iso()}]
                }
            }
            try:
                es_request(f'/{TASK_INDEX}/_update/{task_id}?refresh=true', update_body, method='POST')
                logger.info(f"pool-shutdown: requeued task {task_id}")
            except Exception as e:
                logger.error(f"pool-shutdown: failed to update task {task_id}: {e}")
    except Exception as e:
        logger.error(f"pool-shutdown: failed to release orphaned tasks: {e}")


def _cleanup_ephemeral_temp_dirs() -> None:
    """Broad manager-level cleanup for any stray flume-* temp dirs (belt & suspenders)."""
    tmp_path = Path(tempfile.gettempdir())
    cleaned = 0
    for d in tmp_path.glob("flume-*"):
        if d.is_dir():
            try:
                shutil.rmtree(d)
                cleaned += 1
            except Exception:
                pass
    if cleaned > 0:
        logger.info(f"pool-shutdown: wiped {cleaned} stray ephemeral clone directories")


def perform_graceful_shutdown() -> None:
    """Heavy cleanup executed synchronously by the main thread after the loop breaks."""
    logger.info("manager: executing graceful pool shutdown...")
    start_time = time.time()

    if _WORKER_POOL is not None:
        for name, fut in list(active_futures.items()):
            if not fut.done():
                fut.cancel()  # best effort
        try:
            _WORKER_POOL.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            _WORKER_POOL.shutdown(wait=True)  # fallback for older python versions
        except Exception as e:
            logger.error(f"pool-shutdown: pool shutdown error: {e}")

    try:
        _release_orphaned_tasks()
    except Exception as e:
        logger.error(f"pool-shutdown: task release error: {e}")

    try:
        _cleanup_ephemeral_temp_dirs()
    except Exception as e:
        logger.error(f"pool-shutdown: temp dir cleanup error: {e}")

    elapsed = time.time() - start_time
    logger.info(f"manager: shutdown complete. Took {elapsed:.2f}s. Exiting.")
