"""Worker definition and dynamic scaling for the Flume worker-manager.

Phase 7 Priority 5: Extracted from manager.py.
Contains worker construction, agent role configuration, and dynamic
scaling logic that builds the worker list for each orchestration cycle.

Functions:
    load_agent_role_defs   — Merge per-role overrides from ES or file
    fetch_routing_policy   — Load routing policy for scaling decisions
    get_dynamic_worker_limit — Adaptive per-role concurrency limit
    build_workers          — Build full worker list (cached 60s)
"""
import json
import os
import time
from pathlib import Path
from typing import Optional

from config import (
    NODE_ID, AGENT_MODELS_FILE, AGENT_MODELS_ES_ID,
    ROLE_ORDER,
)
from es.client import es_request
from utils.logger import get_logger
from workspace_llm_env import resolve_cloud_agent_model

logger = get_logger('orchestration.workers')


def log(msg, **kwargs):
    if kwargs:
        logger.info(str(msg), extra={'structured_data': kwargs})
    else:
        logger.info(str(msg))


# ── Agent Role Configuration ────────────────────────────────────────────────

def load_agent_role_defs():
    """Merge per-role overrides from the ES flume-config document (AP-8) or
    the legacy agent_models.json file with LLM_* / EXECUTION_HOST env."""
    import llm_credentials_store as lcs  # noqa: PLC0415

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


# ── Routing Policy & Dynamic Scaling ────────────────────────────────────────

def fetch_routing_policy() -> dict:
    try:
        import httpx  # noqa: PLC0415
        res = es_request('/flume-routing-policy/_doc/singleton', method='GET')
        if res and '_source' in res:
            return res['_source']
    except Exception as e:
        # Only log non-404 errors
        try:
            import httpx as _httpx  # noqa: PLC0415
            if isinstance(e, _httpx.HTTPStatusError) and e.response.status_code == 404:
                pass
            else:
                logger.error(f"Failed to fetch routing policy for worker limit calculations: {e}")
        except Exception:
            logger.error(f"Failed to fetch routing policy for worker limit calculations: {e}")
    return {}


def get_dynamic_worker_limit(node_caps_fn=None) -> int:
    """Return the dynamic number of workers per agent role based on routing policy and mesh size.

    Frontier/Hybrid: Compute boundaries scaled cleanly by host multiprocessing cores.
    Local: Mesh node caps combined algorithmically (max 2 as floor).
    Override with WORKERS_PER_ROLE env var for explicitly forcing concurrency boundaries.

    Args:
        node_caps_fn: Callable that returns node concurrency caps dict.
                      Injected to avoid circular imports with manager.py.
    """
    try:
        policy = fetch_routing_policy()
        mode = policy.get('mode', 'local').lower()

        if mode in ('frontier', 'hybrid'):
            import multiprocessing  # noqa: PLC0415
            cores = multiprocessing.cpu_count()
            limit = max(4, cores * 2)
            logger.debug(f"Dynamic worker scaling [{mode}]: detected {cores} cores, bound limit set to {limit} per role.")
            return limit
        else:
            if node_caps_fn:
                caps = node_caps_fn()
            else:
                caps = {'localhost': 4}
            total_mesh_capacity = sum(caps.values())
            limit = max(2, total_mesh_capacity)
            logger.debug(f"Dynamic worker scaling [local]: {len(caps)} mesh node(s) with {total_mesh_capacity} combined capacity, bound limit set to {limit} per role.")
            return limit
    except Exception as e:
        logger.error(f"Failed dynamic scaling, defaulting to 2: {e}")
        return 2


# ── Worker Builder (Cached) ─────────────────────────────────────────────────

# Phase 2.2: TTL cache for worker list.
# Worker definitions (role defs + dynamic limit) don't change between cycles
# unless the operator edits ES config. Cache for 60s.
_WORKERS_CACHE: dict = {'ts': 0.0, 'data': None}
_WORKERS_CACHE_TTL_SECONDS = 60


def build_workers(force: bool = False, node_caps_fn=None):
    """Build the full worker list from role definitions and dynamic limits.

    Phase 2.2: Results are cached for 60s. Pass force=True after config changes.

    Args:
        node_caps_fn: Callable for node concurrency caps (passed to get_dynamic_worker_limit).
    """
    now = time.time()
    if not force and _WORKERS_CACHE['data'] is not None and (now - _WORKERS_CACHE['ts']) < _WORKERS_CACHE_TTL_SECONDS:
        return _WORKERS_CACHE['data']

    workers = []
    limit = get_dynamic_worker_limit(node_caps_fn=node_caps_fn)
    raw = os.environ.get('WORKERS_PER_ROLE')
    if raw:
        try:
            limit = int(raw)
        except ValueError:
            limit = get_dynamic_worker_limit(node_caps_fn=node_caps_fn)

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
