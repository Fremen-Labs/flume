"""Observability package for the Flume worker-manager.

Provides Prometheus metrics definitions, instrumented wrappers,
and a /metrics endpoint served alongside the existing health server.
"""
from observability.metrics import (  # noqa: F401
    CYCLE_DURATION,
    TASKS_CLAIMED,
    TASKS_DISPATCHED,
    TASKS_COMPLETED,
    CLAIM_LATENCY,
    ES_REQUEST_DURATION,
    ES_REQUEST_ERRORS,
    POOL_WORKERS_ACTIVE,
    POOL_WORKERS_TOTAL,
    SWEEP_REQUEUED,
    SHUTDOWN_DURATION,
)
