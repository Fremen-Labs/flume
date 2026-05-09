"""Prometheus metrics definitions for the Flume worker-manager.

Phase 10: Provides real-time operational visibility into cycle times,
claim latency, ES request performance, pool utilization, and error rates.

All metrics are module-level singletons — import them where needed and
call .observe(), .inc(), .set() etc. directly.

The /metrics endpoint is served via generate_latest() and exposed on
the existing health server (port 8080).
"""
from prometheus_client import Counter, Gauge, Histogram

from config import NODE_ID

# ── Histogram buckets ────────────────────────────────────────────────────────
# Tuned for Flume's operational ranges:
#   - ES requests: 5ms–30s (includes slow _update_by_query)
#   - Cycle: 100ms–60s (full claim+sweep+dispatch)
#   - LLM calls: 1s–120s (model inference latency)
_ES_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_CYCLE_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
_CLAIM_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

# ── Cycle Metrics ────────────────────────────────────────────────────────────
CYCLE_DURATION = Histogram(
    'flume_cycle_duration_seconds',
    'Total duration of one manager cycle (claim + sweep + dispatch)',
    labelnames=['node_id'],
    buckets=_CYCLE_BUCKETS,
)

# ── Task Lifecycle Counters ──────────────────────────────────────────────────
TASKS_CLAIMED = Counter(
    'flume_tasks_claimed_total',
    'Total tasks atomically claimed from ES',
    labelnames=['role', 'node_id'],
)

TASKS_DISPATCHED = Counter(
    'flume_tasks_dispatched_total',
    'Total tasks dispatched to the worker pool',
    labelnames=['role', 'node_id'],
)

TASKS_COMPLETED = Counter(
    'flume_tasks_completed_total',
    'Total tasks completed (success or failure)',
    labelnames=['role', 'success', 'node_id'],
)

# ── Claim Latency ────────────────────────────────────────────────────────────
CLAIM_LATENCY = Histogram(
    'flume_claim_latency_seconds',
    'Time spent in try_atomic_claim() per role',
    labelnames=['role'],
    buckets=_CLAIM_BUCKETS,
)

# ── ES Client Metrics ────────────────────────────────────────────────────────
ES_REQUEST_DURATION = Histogram(
    'flume_es_request_duration_seconds',
    'ES request roundtrip time',
    labelnames=['method', 'endpoint'],
    buckets=_ES_BUCKETS,
)

ES_REQUEST_ERRORS = Counter(
    'flume_es_request_errors_total',
    'Total ES request failures',
    labelnames=['method', 'endpoint'],
)

# ── Worker Pool Gauges ───────────────────────────────────────────────────────
POOL_WORKERS_ACTIVE = Gauge(
    'flume_pool_workers_active',
    'Number of workers currently executing tasks',
    labelnames=['node_id'],
)

POOL_WORKERS_TOTAL = Gauge(
    'flume_pool_workers_total',
    'Total configured pool size',
    labelnames=['node_id'],
)

# ── Sweep Metrics ────────────────────────────────────────────────────────────
SWEEP_REQUEUED = Counter(
    'flume_sweep_requeued_total',
    'Tasks requeued by sweep functions',
    labelnames=['sweep_type'],
)

# ── Shutdown Metrics ─────────────────────────────────────────────────────────
SHUTDOWN_DURATION = Histogram(
    'flume_shutdown_duration_seconds',
    'Time spent in graceful shutdown',
    labelnames=['node_id'],
    buckets=(0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 120.0),
)
