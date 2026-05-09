#!/usr/bin/env python3
import json
import os
import random
import re
import sys
import time
import httpx
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Centralized Configuration ────────────────────────────────────────────────
# Phase 7: All constants and env vars are now in config.py (single source of truth).
from config import (
    NODE_ID, WORKSPACE_ROOT as _WS, WORKER_MANAGER_BASE as BASE,
    ES_URL, ES_API_KEY, ES_VERIFY_TLS, TASK_INDEX, POLL_SECONDS,
    POOL_SIZE as _POOL_SIZE, AGENT_MODELS_FILE, AGENT_MODELS_ES_ID,
    ROLE_ORDER, now_iso,
)

from flume_secrets import apply_runtime_config  # noqa: E402
from workspace_llm_env import resolve_cloud_agent_model, sync_llm_env_from_workspace  # noqa: E402
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
# Phase 7: ES client, telemetry, and pool lifecycle extracted from this file.
from es.client import es_request, es_request_raw, get_es_client
from es.telemetry import log_task_state_transition, log_telemetry_event, flush_telemetry as _flush_telemetry
from pool import (
    get_worker_pool as _get_worker_pool,
    active_futures as _active_futures,
    shutdown_requested as _shutdown_requested_flag,
    shutdown_pool_signal as _shutdown_pool_signal,
    perform_graceful_shutdown as _perform_graceful_shutdown,
)

from orchestration import (
    try_atomic_claim,
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
    from observability.metrics import (
        CYCLE_DURATION, TASKS_CLAIMED, TASKS_DISPATCHED, TASKS_COMPLETED,
        CLAIM_LATENCY, POOL_WORKERS_ACTIVE, POOL_WORKERS_TOTAL,
    )
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


def load_agent_role_defs():
    """Merge per-role overrides from the ES flume-config document (AP-8) or
    the legacy agent_models.json file with LLM_* / EXECUTION_HOST env."""
    default_model = (os.environ.get('LLM_MODEL') or 'llama3.2').strip() or 'llama3.2'
    default_host = (os.environ.get('EXECUTION_HOST') or 'localhost').strip() or 'localhost'
    default_prov = os.environ.get('LLM_PROVIDER', 'ollama').strip().lower()
    cfg = {}
    # 1. Try ES flume-config first (K8s-native, replica-safe)
    try:
        res = es_request(f'/flume-config/_doc/{AGENT_MODELS_ES_ID}', method='GET')
        src = (res or {}).get('_source') or {}
        if src.get('roles'):
            cfg = src['roles']
    except Exception:
        pass  # fall through to file-based fallback
    # 2. File-based fallback (dev / migration period)
    if not cfg and AGENT_MODELS_FILE.is_file():
        try:
            data = json.loads(AGENT_MODELS_FILE.read_text(encoding='utf-8'))
            cfg = (data.get('roles') or {}) if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            pass
    defs = []
    for role in ROLE_ORDER:
        spec = cfg.get(role)
        model = default_model
        host = default_host
        prov = default_prov
        cred_id = ''
        if isinstance(spec, str):
            model = spec.strip() or model
        elif isinstance(spec, dict):
            model = (spec.get('model') or model).strip() or model
            host = (spec.get('executionHost') or host).strip() or host
            prov = (spec.get('provider') or prov).strip().lower() or prov
            cred_id = str(spec.get('credentialId') or spec.get('credential_id') or '').strip()
        if not cred_id:
            cred_id = lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
        model = resolve_cloud_agent_model(prov, model, default_model)
        defs.append(
            {
                'role': role,
                'model': model,
                'execution_host': host,
                'llm_provider': prov,
                'llm_credential_id': cred_id,
            }
        )
    return defs


def fetch_routing_policy() -> dict:
    try:
        res = es_request('/flume-routing-policy/_doc/singleton', method='GET')
        if res and '_source' in res:
            return res['_source']
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            _manager_logger.error(f"Failed to fetch routing policy for worker limit calculations: {e}")
    except Exception as e:
        _manager_logger.error(f"Failed to fetch routing policy for worker limit calculations: {e}")
    return {}

def get_dynamic_worker_limit() -> int:
    """Return the dynamic number of workers per agent role based on routing policy and mesh size.

    Frontier/Hybrid: Compute boundaries scaled cleanly by host multiprocessing cores.
    Local: Mesh node caps combined algorithmically (max 2 as floor).
    Override with WORKERS_PER_ROLE env var for explicitly forcing concurrency boundaries.
    """
    try:
        policy = fetch_routing_policy()
        mode = policy.get('mode', 'local').lower()
        
        if mode in ('frontier', 'hybrid'):
            import multiprocessing
            cores = multiprocessing.cpu_count()
            limit = max(4, cores * 2)
            _manager_logger.debug(f"Dynamic worker scaling [{mode}]: detected {cores} cores, bound limit set to {limit} per role.")
            return limit
        else:
            caps = _fetch_node_concurrency_caps()
            total_mesh_capacity = sum(caps.values())
            limit = max(2, total_mesh_capacity)
            _manager_logger.debug(f"Dynamic worker scaling [local]: {len(caps)} mesh node(s) with {total_mesh_capacity} combined capacity, bound limit set to {limit} per role.")
            return limit
    except Exception as e:
        _manager_logger.error(f"Failed dynamic scaling, defaulting to 2: {e}")
        return 2


# Phase 2.2: TTL cache for worker list.
# Worker definitions (role defs + dynamic limit) don't change between cycles
# unless the operator edits ES config. Cache for 60s.
_WORKERS_CACHE: dict = {'ts': 0.0, 'data': None}
_WORKERS_CACHE_TTL_SECONDS = 60


def build_workers(force: bool = False):
    """Build the full worker list from role definitions and dynamic limits.

    Phase 2.2: Results are cached for 60s. Pass force=True after config changes.
    """
    now = time.time()
    if not force and _WORKERS_CACHE['data'] is not None and (now - _WORKERS_CACHE['ts']) < _WORKERS_CACHE_TTL_SECONDS:
        return _WORKERS_CACHE['data']

    workers = []
    limit = get_dynamic_worker_limit()
    raw = os.environ.get('WORKERS_PER_ROLE')
    if raw:
        try:
            limit = int(raw)
        except ValueError:
            limit = get_dynamic_worker_limit()

    for role_def in load_agent_role_defs():
        active_limit = 1 if role_def['role'] == 'pm' else limit
        for idx in range(1, active_limit + 1):
            workers.append(
                {
                    'name': f"{role_def['role']}-{NODE_ID}-worker-{idx}",
                    'role': role_def['role'],
                    'model': role_def['model'],
                    'execution_host': role_def['execution_host'],
                    'llm_provider': role_def['llm_provider'],
                    'llm_credential_id': role_def.get('llm_credential_id') or '',
                }
            )

    _WORKERS_CACHE['ts'] = now
    _WORKERS_CACHE['data'] = workers
    return workers

# Phase 7: es_request() extracted to es/client.py.




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

    workers = build_workers()
    
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

def sync_worker_processes(state):
    """Dispatch claimed workers to the process pool and harvest completed futures."""
    from worker_handlers import execute_worker_task

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
# Phase 7: Pool lifecycle, shutdown signal, orphan task requeue, and temp
# cleanup extracted to pool.py. Imported at top of file.


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
