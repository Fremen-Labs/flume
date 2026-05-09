"""Worker process dispatch and future harvesting for the Flume worker-manager.

Phase 7 Priority 8: Extracted from manager.py.
Contains the dispatch loop that submits claimed workers to the
ProcessPoolExecutor and harvests completed futures for telemetry.

Functions:
    sync_worker_processes — Dispatch claimed workers and harvest completed futures
"""
from config import NODE_ID, POOL_SIZE as _POOL_SIZE
from pool import (
    get_worker_pool as _get_worker_pool,
    active_futures as _active_futures,
)
from utils.logger import get_logger

logger = get_logger('orchestration.dispatch')


def log(msg, **kwargs):
    if kwargs:
        logger.info(str(msg), extra={'structured_data': kwargs})
    else:
        logger.info(str(msg))


# ── Prometheus Instrumentation (Phase 10) ────────────────────────────────────
try:
    from observability.metrics import (
        TASKS_DISPATCHED, TASKS_COMPLETED,
        POOL_WORKERS_ACTIVE, POOL_WORKERS_TOTAL,
    )
    _METRICS_ENABLED = True
except ImportError:
    _METRICS_ENABLED = False


# ── Dispatch Loop ────────────────────────────────────────────────────────────

def sync_worker_processes(state):
    """Dispatch claimed workers to the process pool and harvest completed futures.

    This function bridges the orchestration loop with the ProcessPoolExecutor:
    1. Harvests completed futures, logging results and updating Prometheus counters.
    2. Submits new work for any claimed workers not already in-flight.

    Called from ``cycle()`` after state has been saved to ES.
    """
    from worker_handlers import execute_worker_task  # noqa: PLC0415

    # Phase 10: Update pool gauge
    if _METRICS_ENABLED:
        POOL_WORKERS_ACTIVE.labels(node_id=NODE_ID).set(len(_active_futures))
        POOL_WORKERS_TOTAL.labels(node_id=NODE_ID).set(_POOL_SIZE)

    # 1. Harvest completed futures
    completed = [name for name, fut in list(_active_futures.items()) if fut.done()]
    for name in completed:
        try:
            fut = _active_futures.pop(name)
            result = fut.result(timeout=0)
            if result and result.get('error'):
                log(f"pool-worker [{name}] finished with error: {result['error'][:200]}")
                if _METRICS_ENABLED:
                    TASKS_COMPLETED.labels(role=result.get('role', 'unknown'), success='false', node_id=NODE_ID).inc()
            elif result:
                log(f"pool-worker [{name}] completed task={result.get('task_id')} success={result.get('success')}")
                if _METRICS_ENABLED:
                    _success = 'true' if result.get('success') else 'false'
                    TASKS_COMPLETED.labels(role=result.get('role', 'unknown'), success=_success, node_id=NODE_ID).inc()
        except Exception as e:
            log(f"pool-worker [{name}] future error: {e}")
            if _METRICS_ENABLED:
                TASKS_COMPLETED.labels(role='unknown', success='false', node_id=NODE_ID).inc()

    # 2. Submit new work for claimed workers not already in-flight
    claimed = [w for w in state.get('workers', []) if w.get('status') == 'claimed']
    for w in claimed:
        name = w.get('name')
        if not name:
            continue
        if name in _active_futures:
            continue  # already running in pool

        _active_futures[name] = _get_worker_pool().submit(execute_worker_task, dict(w))
        log(f"manager: dispatched [{name}] to worker pool (pool_size={_POOL_SIZE}, in_flight={len(_active_futures)})")
        if _METRICS_ENABLED:
            _dispatch_role = w.get('role', 'unknown')
            TASKS_DISPATCHED.labels(role=_dispatch_role, node_id=NODE_ID).inc()
