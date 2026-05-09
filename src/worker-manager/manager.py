#!/usr/bin/env python3
import os
import sys
import time
import httpx
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Centralized Configuration ────────────────────────────────────────────────
# Phase 7: All constants and env vars are now in config.py (single source of truth).
from config import (
    NODE_ID, WORKSPACE_ROOT as _WS,
    ES_URL, ES_API_KEY, ES_VERIFY_TLS, TASK_INDEX, POLL_SECONDS,
    POOL_SIZE as _POOL_SIZE, now_iso,
)

from flume_secrets import apply_runtime_config  # noqa: E402
from workspace_llm_env import sync_llm_env_from_workspace  # noqa: E402
import llm_credentials_store as lcs  # noqa: E402
from utils.workspace import resolve_safe_workspace  # noqa: E402

# Hydrate dashboard LLM settings into env if available
try:
    from dashboard.llm_settings import load_effective_pairs
    for _k, _v in load_effective_pairs(resolve_safe_workspace()).items():
        if _v is not None and str(_v).strip():
            os.environ[_k] = str(_v).strip()
except ImportError:
    pass

# ── Extracted Modules ────────────────────────────────────────────────────────
# Phase 7: ES client, telemetry, pool lifecycle, orchestration extracted.
from es.client import es_request
from es.telemetry import log_telemetry_event, flush_telemetry as _flush_telemetry
from pool import (
    shutdown_pool_signal as _shutdown_pool_signal,
    perform_graceful_shutdown as _perform_graceful_shutdown,
)
from orchestration import (
    try_atomic_claim,
    build_workers,
    sync_worker_processes,
    requeue_stuck_implementer_tasks,
    requeue_stuck_review_tasks,
    promote_planned_tasks,
    execute_block_sweep,
    execute_resume_sweep,
    count_available_by_status,
    SWEEP_LAST_RUN as _SWEEP_LAST_RUN,
    SWEEP_INTERVALS as _SWEEP_INTERVALS,
)

# AP-6: file_path logger arg removed — get_logger writes to stdout only.
from utils.logger import get_logger  # noqa: E402
_manager_logger = get_logger('worker-manager')


# Phase 2.2: TTL cache for node concurrency caps.
# Node registry doesn't change between cycles — cache for 60s.
_NODE_CAPS_CACHE: dict = {'ts': 0.0, 'data': None}
_NODE_CAPS_TTL_SECONDS = 60


def _fetch_node_concurrency_caps(force: bool = False) -> dict:
    """Dynamically determine PER-NODE MAX_CONCURRENT_TASKS based on cluster constraints.

    Phase 2.2: Results are cached for 60s since node registry changes are rare.
    Pass force=True to bypass the cache (e.g. after a config change).
    """
    now = time.time()
    if not force and _NODE_CAPS_CACHE['data'] is not None and (now - _NODE_CAPS_CACHE['ts']) < _NODE_CAPS_TTL_SECONDS:
        return _NODE_CAPS_CACHE['data']

    node_caps = {}
    try:
        res = es_request('/flume-node-registry/_search', {'size': 100}, method='GET')
        nodes = res.get('hits', {}).get('hits', []) if res else []
        for n in nodes:
            src = n.get('_source', {})
            node_id = src.get('id', n.get('_id'))
            
            node_cap = src.get('concurrency_cap', 4)
            health = src.get('health', {})
            latency = health.get('latency_ms', 0)
            
            # Emergency Brake: Gradual ramp-down limit instead of hard clamping to 1
            if latency > 20000:
                _manager_logger.critical(f"EMERGENCY BRAKE: Severe latency on {node_id} ({latency}ms). Ramping limit down by 1.")
                node_cap = max(1, node_cap - 1)
                
            node_caps[node_id] = node_cap

    except Exception as e:
        _manager_logger.error(f"Failed calculating adaptive per-node concurrency: {e}")
        # On error, return stale cache if available rather than empty dict
        if _NODE_CAPS_CACHE['data'] is not None:
            return _NODE_CAPS_CACHE['data']
        
    # Default fallback for unknown locals
    if 'localhost' not in node_caps:
        node_caps['localhost'] = 4

    _NODE_CAPS_CACHE['ts'] = now
    _NODE_CAPS_CACHE['data'] = node_caps
    return node_caps

def log(msg, **kwargs):
    if kwargs:
        _manager_logger.info(str(msg), extra={'structured_data': kwargs})
    else:
        _manager_logger.info(str(msg))

# Phase 7: Telemetry buffer, es_request_raw, log_task_state_transition,
# and log_telemetry_event extracted to es/telemetry.py.


# ── Prometheus Instrumentation (Phase 10) ────────────────────────────────────
try:
    from observability.metrics import CYCLE_DURATION
    _METRICS_ENABLED = True
except ImportError:
    _METRICS_ENABLED = False


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","service":"flume-worker"}')
        elif self.path == '/metrics':
            try:
                from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
                output = generate_latest()
                self.send_response(200)
                self.send_header("Content-Type", CONTENT_TYPE_LATEST)
                self.end_headers()
                self.wfile.write(output)
            except ImportError:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"prometheus_client not installed"}')
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, format, *args):
        pass

def start_health_server():
    server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()



def save_state(state):
    try:
        state['updated_at'] = now_iso()
        es_request(f'/agent-system-workers/_doc/{NODE_ID}', state, method='POST')
    except Exception as e:
        log(f"Error publishing worker state to ES: {e}")




def cycle():
    """Main orchestration heartbeat tick"""
    _cycle_start = time.monotonic()
    # 1. Check Global Cluster Paused state
    is_paused = False
    try:
        clust = es_request('/agent-system-cluster/_doc/config', method='GET')
        if clust and clust.get('_source', {}).get('status') == 'paused':
            is_paused = True
    except Exception:
        pass

    sync_llm_env_from_workspace(_WS)

    # Phase 2.1: Sweep throttling.
    # Stuck-task requeue scans all running/review tasks but only matters every
    # 30s (thresholds are 300-600s). promote_planned runs every 5s since it
    # directly affects pipeline throughput. Skipping when not due saves ~2 ES
    # calls per cycle for requeue and ~3-10 for promote.
    now_ts = time.time()
    if now_ts - _SWEEP_LAST_RUN.get('stuck_impl', 0) >= _SWEEP_INTERVALS['stuck_impl']:
        _SWEEP_LAST_RUN['stuck_impl'] = now_ts
        try:
            rq = requeue_stuck_implementer_tasks()
            if rq:
                log(f"stuck-implementer sweep: requeued {rq} task(s)")
        except Exception as e:
            log(f"stuck-implementer sweep error: {e}")

    if now_ts - _SWEEP_LAST_RUN.get('stuck_review', 0) >= _SWEEP_INTERVALS['stuck_review']:
        _SWEEP_LAST_RUN['stuck_review'] = now_ts
        try:
            rq_rev = requeue_stuck_review_tasks()
            if rq_rev:
                log(f"stuck-review sweep: cleared {rq_rev} phantom lock(s)")
        except Exception as e:
            log(f"stuck-review sweep error: {e}")

    if now_ts - _SWEEP_LAST_RUN.get('promote', 0) >= _SWEEP_INTERVALS['promote']:
        _SWEEP_LAST_RUN['promote'] = now_ts
        try:
            promoted = promote_planned_tasks()
            if promoted:
                log(f"dependency sweep: promoted {promoted} task(s) to ready")
        except Exception as e:
            log(f"dependency sweep error: {e}")

    # Phase 1.1: Pre-flight availability counts.
    # One _msearch roundtrip tells us how many tasks exist per status.
    # Workers for roles with zero available tasks skip try_atomic_claim entirely,
    # eliminating ~16 wasted _update_by_query calls per cycle.
    available_counts = count_available_by_status()

    busy_workers = {}
    try:
        res = es_request(
            f'/{TASK_INDEX}/_search',
            {'size': 500, 'query': {'match': {'queue_state': 'active'}}},
            method='POST'
        )
        for h in res.get('hits', {}).get('hits', []):
            s = h.get('_source', {})
            wn = s.get('active_worker')
            if wn:
                busy_workers[wn] = {
                    'task_id': s.get('id', h.get('_id')), 
                    'task_title': s.get('title'),
                    'execution_host': s.get('execution_host')
                }
    except Exception as e:
        log(f"error fetching busy workers: {e}")

    workers = build_workers(node_caps_fn=_fetch_node_concurrency_caps)
    
    cloud_providers = {'openai', 'anthropic', 'google', 'azure'}
    node_loads = {}
    active_mesh_count = 0
    
    # Pre-calculate active loads dynamically natively per-node
    for wn, wdat in busy_workers.items():
        matching_worker = next((w for w in workers if w['name'] == wn), None)
        if matching_worker:
            w_prov = (matching_worker.get('llm_provider') or 'ollama').lower()
            if w_prov not in cloud_providers:
                h = wdat.get('execution_host') or matching_worker.get('execution_host') or 'localhost'
                node_loads[h] = node_loads.get(h, 0) + 1
                
    node_caps = _fetch_node_concurrency_caps()
    
    # Auto-resume sweep to recover Blocked tasks natively ensuring global mesh unlocks
    execute_resume_sweep()
    execute_block_sweep(node_loads, node_caps, cloud_providers)
    
    state = {'updated_at': now_iso(), 'workers': []}
    for worker in workers:
        snapshot = dict(worker)
        snapshot['heartbeat_at'] = now_iso()
        
        if worker['name'] in busy_workers:
            snapshot['status'] = 'claimed'
            snapshot['current_task_id'] = busy_workers[worker['name']]['task_id']
            snapshot['current_task_title'] = busy_workers[worker['name']]['task_title']
            
            # Adopt the dynamic execution host provided by Gateway / Node Mesh
            if busy_workers[worker['name']].get('execution_host'):
                snapshot['execution_host'] = busy_workers[worker['name']]['execution_host']
                
            wcid = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            snapshot['preferred_llm_credential_id'] = wcid
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, wcid)
            state['workers'].append(snapshot)
            continue
            
        snapshot['status'] = 'idle'
        snapshot['current_task_id'] = None
        if is_paused:
            # Cluster is globally disabled. Heartbeat state as idle but do not pull new tasks
            wcid = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            snapshot['preferred_llm_credential_id'] = wcid
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, wcid)
            state['workers'].append(snapshot)
            continue

        pref_model = worker['model']
        pref_prov = worker.get('llm_provider')
        pref_cred = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID

        check_prov = (pref_prov or 'ollama').lower()
        active_host = worker.get('execution_host') or 'localhost'
        host_load = node_loads.get(active_host, 0)
        host_cap = node_caps.get(active_host, 4)
        
        if check_prov not in cloud_providers and host_load >= host_cap:
            snapshot['status'] = 'idle'
            wcid = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            snapshot['preferred_llm_credential_id'] = wcid
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, wcid)
            state['workers'].append(snapshot)
            log(f"{worker['name']} throttled to protect local Node {active_host} (Node Cap={host_cap})", metric_id="flume_concurrency_throttled_total")
            continue

        # Phase 1.1: Skip claim when no tasks exist for this role's target status.
        # Maps role → target status that try_atomic_claim would search.
        role_target = {
            'pm': 'planned',
            'tester': 'review',
            'reviewer': 'review',
        }.get(worker['role'], 'ready')
        if available_counts.get(role_target, 0) <= 0:
            wcid = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            snapshot['preferred_llm_credential_id'] = wcid
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, wcid)
            state['workers'].append(snapshot)
            continue

        claimed_task = try_atomic_claim(
            worker['role'],
            worker['name'],
            worker.get('execution_host'),
            pref_model,
            pref_prov,
            pref_cred,
        )
        if claimed_task:
            if check_prov not in cloud_providers:
                active_mesh_count += 1
            src = claimed_task.get('_source', {})
            snapshot['status'] = 'claimed'
            snapshot['current_task_id'] = src.get('id', claimed_task.get('_id'))
            snapshot['current_task_title'] = src.get('title')
            snapshot['preferred_model'] = pref_model
            snapshot['preferred_llm_provider'] = pref_prov
            snapshot['preferred_llm_credential_id'] = pref_cred
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, pref_cred)
            log(f"{worker['name']} claimed {snapshot['current_task_id']}")
            log_telemetry_event(worker['name'], "TASK_CLAIM", f"Claimed task: {(snapshot['current_task_title'] or '')[:50]}")
        else:
            snapshot['status'] = 'idle'
            wcid = (worker.get('llm_credential_id') or '').strip() or lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            snapshot['preferred_llm_credential_id'] = wcid
            snapshot['llm_credential_label'] = lcs.resolve_credential_label(_WS, wcid)
        state['workers'].append(snapshot)
    
    save_state(state)
    _flush_telemetry()
    if not is_paused:
        sync_worker_processes(state)

    # Phase 10: Observe total cycle duration
    if _METRICS_ENABLED:
        CYCLE_DURATION.labels(node_id=NODE_ID).observe(time.monotonic() - _cycle_start)

# Phase 7: Pool lifecycle, shutdown signal, orphan task requeue, and temp
# cleanup extracted to pool.py. Imported at top of file.
# Phase 7: Worker dispatch and future harvesting extracted to
# orchestration/dispatch.py. Imported at top of file.


def main():
    import signal
    import pool as _pool_module  # for mutable shutdown_requested flag
    signal.signal(signal.SIGTERM, _shutdown_pool_signal)
    signal.signal(signal.SIGINT, _shutdown_pool_signal)

    apply_runtime_config(_WS)
    from flume_secrets import hydrate_secrets_from_openbao
    hydrate_secrets_from_openbao()
    if 'https' in ES_URL and (not os.environ.get("ES_API_KEY") or os.environ.get("ES_API_KEY") == 'AUTO_GENERATED_BY_INSTALLER'):
        if not os.environ.get("FLUME_ELASTIC_PASSWORD"):
            raise SystemExit(
                'ES_API_KEY or FLUME_ELASTIC_PASSWORD is required for TLS clusters. Store it in OpenBao (KV secret/flume) or .env'
            )
        
    def ping_local_llm():
        raw = os.environ.get("LLM_BASE_URL") or os.environ.get("LOCAL_OLLAMA_BASE_URL", "http://host.docker.internal:11434")
        # Strip /v1 suffix — Ollama's native API endpoints are at /api/*, not /v1/api/*
        url = raw.rstrip('/').removesuffix('/v1')
        if "docker" in url and sys.platform.startswith("linux"):
            log("host.docker.internal natively detected on Linux!", event="linux_network_warning", url=url, advice="define LOCAL_LLM_HOST=172.17.0.1 in .env")
        try:
            httpx.get(f"{url}/api/tags", timeout=3.0)
        except Exception as e:
            log("Local LLM boot ping failed", event="llm_ping_failure", url=url, error=str(e), advice="Workers may stall if unreachable")

    ping_local_llm()
    log('worker manager starting')
    
    while not _pool_module.shutdown_requested:
        try:
            cycle()
        except Exception as e:
            log(f'cycle error: {e}')
        
        # Sleep in small increments to respond to signals faster
        SIGNAL_CHECK_INTERVAL = 0.1
        for _ in range(int(POLL_SECONDS / SIGNAL_CHECK_INTERVAL)):
            if _pool_module.shutdown_requested:
                break
            time.sleep(SIGNAL_CHECK_INTERVAL)

    _perform_graceful_shutdown()


if __name__ == '__main__':
    from ast_poller import start_poller_thread  # type: ignore
    start_health_server()
    start_poller_thread()
    main()
