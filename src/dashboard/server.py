#!/usr/bin/env python3
# ruff: noqa: E402
"""Flume Server — Central intelligence and frontend orchestration."""
from pathlib import Path
from typing import Optional
import json
import os
import re
import shlex
import signal
import sys
import threading
import time
import httpx

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded



import subprocess
import urllib.request
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

# Flume Bootstrap Logic


# --- Legacy Env ---
BASE = Path(__file__).resolve().parent
_SRC_ROOT = BASE.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))
# Dashboard modules (llm_settings, agent_models_settings) live next to server.py; prefer this package on import.
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from utils.logger import get_logger
logger = get_logger(__name__)

from flume_secrets import apply_runtime_config, hydrate_secrets_from_openbao  # type: ignore  # noqa: E402

# Merge .env config
apply_runtime_config(_SRC_ROOT)

# Hydrate OpenBao Secrets Natively
hydrate_secrets_from_openbao()

# ES index creation is centralized in the CLI `flume start` orchestrator.
# The dashboard only verifies indices exist at startup — it does NOT create them.
# This eliminates the boot-race where workers hit 404s before the dashboard finishes bootstrapping.
from utils.logger import get_logger as _get_startup_logger
from utils.exceptions import SAFE_EXCEPTIONS

_startup_logger = _get_startup_logger('es_bootstrap')
try:
    import urllib.request as _ur
    import urllib.error as _ue
    from config import get_settings
    _s = get_settings()
    _es_check_url = _s.ES_URL or ('http://elasticsearch:9200' if _s.FLUME_NATIVE_MODE != '1' else 'http://localhost:9200')
    _check_req = _ur.Request(f"{_es_check_url}/agent-task-records", method='HEAD')
    # Add auth + TLS for hardened ES
    from core.elasticsearch import _get_auth_headers as _boot_auth, ctx as _boot_ctx
    for _k, _v in _boot_auth().items():
        _check_req.add_header(_k, _v)
    with _ur.urlopen(_check_req, timeout=3, context=_boot_ctx) as _r:
        if _r.status == 200:
            _startup_logger.info("ES index verification passed — agent-task-records exists")
except _ue.HTTPError as _e:
    if _e.code == 404:
        _startup_logger.warning("ES index 'agent-task-records' not found — was `flume start` used to boot?")
except SAFE_EXCEPTIONS as _e:
    _startup_logger.warning(f"ES index verification skipped — cannot reach Elasticsearch: {_e}")


# ES configuration imported from core.elasticsearch (single source of truth)
from core.elasticsearch import ES_API_KEY, ES_URL, _get_auth_headers, ctx as _es_ctx


def _seed_llm_config_from_env() -> None:
    """
    Fallback boot-time seed for flume-llm-config/singleton.

    The CLI's SeedLLMConfig() runs on the host machine against localhost:9200
    before the dashboard container starts. On macOS Docker Desktop the port-forward
    can have a brief lag causing that write to timeout silently. This function
    runs inside the container (where ES is always reachable via the Docker service
    name) and writes LLM_MODEL / LLM_PROVIDER / LLM_BASE_URL from the container
    env vars using doc_as_upsert — so it ONLY fills in missing fields and NEVER
    overwrites a value the user already saved via the Settings UI.
    """
    import urllib.request
    import urllib.error
    from config import get_settings
    _s = get_settings()
    model = _s.LLM_MODEL.strip()
    provider = _s.LLM_PROVIDER.strip()
    base_url = _s.LLM_BASE_URL.strip()

    if not model and not provider:
        logger.debug('_seed_llm_config_from_env: no LLM_MODEL or LLM_PROVIDER in env — skipping')
        return

    # Build upsert payload only from non-empty env values
    doc: dict = {}
    if model:
        doc['LLM_MODEL'] = model
    if provider:
        doc['LLM_PROVIDER'] = provider
    if base_url:
        doc['LLM_BASE_URL'] = base_url

    # Use the update API with detect_noop=true so ES ignores no-op writes.
    # doc_as_upsert creates the doc if absent; otherwise only merges missing fields
    # because we explicitly do NOT overwrite here — we only supply missing values.
    try:
        es_url_local = ES_URL
        url = f'{es_url_local}/flume-llm-config/_update/singleton'
        headers: dict = {'Content-Type': 'application/json'}
        headers.update(_get_auth_headers())

        # Fetch first to check if values already set — never clobber user changes
        get_req = urllib.request.Request(
            f'{es_url_local}/flume-llm-config/_doc/singleton',
            headers=headers, method='GET',
        )
        try:
            with urllib.request.urlopen(get_req, timeout=5, context=_es_ctx) as r:
                import json as _json
                existing_src = _json.loads(r.read()).get('_source', {})
                # Remove fields already present in ES so we don't overwrite them
                for k in list(doc.keys()):
                    if existing_src.get(k):
                        doc.pop(k, None)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                logger.warning(f'_seed_llm_config_from_env: GET failed ({e}) — proceeding with full upsert')
        except SAFE_EXCEPTIONS as e:
            logger.warning(f'_seed_llm_config_from_env: GET error ({e}) — proceeding with full upsert')

        if not doc:
            logger.info('_seed_llm_config_from_env: all LLM fields already present in ES — nothing to seed')
            return

        body = json.dumps({'doc': doc, 'doc_as_upsert': True}).encode()
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=5, context=_es_ctx) as r:
            logger.info(f'_seed_llm_config_from_env: seeded {list(doc.keys())} → flume-llm-config (model={model})')
    except SAFE_EXCEPTIONS as e:
        logger.warning(f'_seed_llm_config_from_env: non-fatal failure — {e}')

from config import get_settings
_s = get_settings()
HOST = _s.DASHBOARD_HOST
PORT = int(_s.DASHBOARD_PORT)
# Pre-built Vite output only — editing src/frontend/src/*.tsx requires: ./flume build-ui (see install/README.md).
STATIC_ROOT = Path(__file__).resolve().parent.parent / 'frontend' / 'dist'

from utils.workspace import resolve_safe_workspace, WorkspaceInitializationError

# Module-level paths are bounded to block AppSec Path Traversals seamlessly isolating the host
WORKSPACE_ROOT = resolve_safe_workspace()

# AP-2 resolved: WORKER_STATE removed — worker lifecycle state belongs in ES (flume-workers index).
# AP-9 resolved: SESSIONS_DIR removed — plan sessions already fully migrated to agent-plan-sessions ES index.
# AP-3 resolved: PROJECTS_REGISTRY removed — projects.json migration is complete; sentinel logic deleted.

LLM_BASE_URL = _s.LLM_BASE_URL
LLM_MODEL = _s.LLM_MODEL

# AP-1: Sequence counters are now stored atomically in the ES `flume-counters` index.
# One document per prefix (e.g. 'task', 'epic'); field `value` = highest allocated N.
# See es_counter_increment() and es_counter_hwm() below.
COUNTERS_INDEX = 'flume-counters'



from core.projects_store import (
    load_projects_registry,
)


from core.elasticsearch import (
    es_search,
    es_upsert,
    es_post,
    es_bulk_update_proxy,
    _es_bulk_flusher_loop,
)


def _lazy_append_task_agent_log_note(es_id: str, note: str) -> bool:
    from api.tasks import _append_task_agent_log_note
    return _append_task_agent_log_note(es_id, note)


def _sync_llm_runtime_env():
    try:
        from workspace_llm_env import sync_llm_env_from_workspace  # type: ignore

        sync_llm_env_from_workspace(WORKSPACE_ROOT)
    except SAFE_EXCEPTIONS:
        logger.debug("sync_llm_env_from_workspace: failed on startup (non-critical)", exc_info=True)

# --- Extracted Domain: Planning ---

# --- Extracted Domain: Tasks ---
from core.tasks import (
    load_workers,
    git_repo_info
)


async def load_repos(registry=None):
    """Return git_repo_info for locally-mounted projects only.

    AP-12: Remote/indexed projects have no persistent local clone — they are
    served via GitHostClient REST API. Silently falling back to a workspace
    path for non-local projects was masking "missing clone" bugs and creating
    spurious filesystem activity on the bind-mount.
    """
    registry = registry if registry is not None else load_projects_registry()
    repos = []
    for p in registry:
        local_path = p.get('path') or ''
        cs = p.get('clone_status') or ''
        if not local_path or cs not in ('local',):
            # Remote/indexed/no_repo projects don't have a local clone.
            # They appear in the dashboard via ES data only.
            continue
        repos.append(await git_repo_info(p['id'], Path(local_path)))
    return repos


_SNAPSHOT_CACHE_DATA = None
_SNAPSHOT_CACHE_TIME = 0.0


def _merge_recent_task_hits_with_blocked(recent_hits: list, blocked_hits: list) -> list:
    """
    Snapshot caps recent activity (300) for performance. Blocked tasks must always be
    visible for triage — merge in any blocked docs not already in the recent slice.
    """
    by_id: dict = {}
    order: list = []
    for h in recent_hits:
        src = h.get('_source') or {}
        tid = src.get('id') or h.get('_id')
        if not tid:
            continue
        k = str(tid)
        if k not in by_id:
            order.append(k)
        by_id[k] = h
    for h in blocked_hits:
        src = h.get('_source') or {}
        tid = src.get('id') or h.get('_id')
        if not tid:
            continue
        k = str(tid)
        if k in by_id:
            continue
        order.append(k)
        by_id[k] = h
    return [by_id[k] for k in order]


async def load_snapshot():
    global _SNAPSHOT_CACHE_DATA, _SNAPSHOT_CACHE_TIME
    now = time.time()
    if _SNAPSHOT_CACHE_DATA and (now - _SNAPSHOT_CACHE_TIME) < 2.0:
        return _SNAPSHOT_CACHE_DATA

    if not ES_API_KEY or ES_API_KEY == 'AUTO_GENERATED_BY_INSTALLER':
        pass

    with ThreadPoolExecutor(max_workers=7) as pool:
        f_tasks = pool.submit(lambda: es_search('agent-task-records', {
            'size': 300,
            'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': {
                'bool': {
                    'must': [{'match_all': {}}],
                    'must_not': [{'term': {'status': 'archived'}}],
                }
            },
        }))
        f_blocked_tasks = pool.submit(lambda: es_search('agent-task-records', {
            'size': 500,
            'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': {
                'bool': {
                    'must': [{'term': {'status': 'blocked'}}],
                    'must_not': [{'term': {'status': 'archived'}}],
                }
            },
        }))
        f_reviews = pool.submit(lambda: es_search('agent-review-records', {
            'size': 100,
            'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': {'match_all': {}}
        }))
        f_failures = pool.submit(lambda: es_search('agent-failure-records', {
            'size': 100,
            'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': {'match_all': {}}
        }))
        f_provenance = pool.submit(lambda: es_search('agent-provenance-records', {
            'size': 100,
            'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': {'match_all': {}}
        }))
        def fetch_savings():
            try:
                agg_res = es_search('agent-token-telemetry', {
                    'size': 0,
                    'aggs': {
                        'total_elastro_savings': {'sum': {'field': 'savings'}},
                        'total_baseline_tokens': {'sum': {'field': 'baseline_tokens'}},
                        'total_baseline_full_context': {'sum': {'field': 'baseline_full_context_tokens'}},
                        'total_actual_tokens': {'sum': {'field': 'actual_tokens_sent'}},
                        'total_input_tokens': {'sum': {'field': 'input_tokens'}},
                        'total_output_tokens': {'sum': {'field': 'output_tokens'}},
                        'by_worker': {
                            'terms': {'field': 'worker_name', 'size': 100},
                            'aggs': {
                                'input': {'sum': {'field': 'input_tokens'}},
                                'output': {'sum': {'field': 'output_tokens'}},
                                'role': {'terms': {'field': 'worker_role'}}
                            }
                        }
                    }
                })
                aggs = agg_res.get('aggregations', {})
                cost_in = float(os.environ.get('FLUME_COST_PER_1K_INPUT', '0.002'))
                cost_out = float(os.environ.get('FLUME_COST_PER_1K_OUTPUT', '0.010'))
                t_in = int(aggs.get('total_input_tokens', {}).get('value', 0))
                t_out = int(aggs.get('total_output_tokens', {}).get('value', 0))
                
                historical_burn = []
                for b in aggs.get('by_worker', {}).get('buckets', []):
                    historical_burn.append({
                        'worker_name': b['key'],
                        'input_tokens': int(b.get('input', {}).get('value', 0)),
                        'output_tokens': int(b.get('output', {}).get('value', 0)),
                        'role': b.get('role', {}).get('buckets', [{'key': 'unknown'}])[0]['key'] if len(b.get('role', {}).get('buckets', [])) > 0 else 'unknown'
                    })

                return {
                    'savings': int(aggs.get('total_elastro_savings', {}).get('value', 0)),
                    'baseline_tokens': int(aggs.get('total_baseline_tokens', {}).get('value', 0)),
                    'baseline_full_context_tokens': int(aggs.get('total_baseline_full_context', {}).get('value', 0)),
                    'actual_tokens_sent': int(aggs.get('total_actual_tokens', {}).get('value', 0)),
                    'total_input_tokens': t_in,
                    'total_output_tokens': t_out,
                    'estimated_cost_usd': round((t_in / 1000.0 * cost_in) + (t_out / 1000.0 * cost_out), 4),
                    'historical_burn': historical_burn,
                }
            except SAFE_EXCEPTIONS:
                logger.debug("api_snapshot: token savings computation failed (best-effort)", exc_info=True)
                return {
                    'savings': 0,
                    'baseline_tokens': 0,
                    'baseline_full_context_tokens': 0,
                    'actual_tokens_sent': 0,
                    'total_input_tokens': 0,
                    'total_output_tokens': 0,
                    'estimated_cost_usd': 0.0,
                    'historical_burn': [],
                }
        f_savings = pool.submit(fetch_savings)
        f_workers = pool.submit(load_workers)
        f_projects = pool.submit(load_projects_registry)

        tasks_recent = f_tasks.result().get('hits', {}).get('hits', [])
        tasks_blocked_extra = f_blocked_tasks.result().get('hits', {}).get('hits', [])
        tasks_res = _merge_recent_task_hits_with_blocked(tasks_recent, tasks_blocked_extra)
        reviews_res = f_reviews.result().get('hits', {}).get('hits', [])
        failures_res = f_failures.result().get('hits', {}).get('hits', [])
        provenance_res = f_provenance.result().get('hits', {}).get('hits', [])
        token_metrics = f_savings.result()
        workers_res = f_workers.result()
        projects_res = f_projects.result()

    repos_res = await load_repos(registry=projects_res)
    from api.projects import _map_task_hit_for_api

    result = {
        'workers': workers_res,
        'tasks': [_map_task_hit_for_api(h) for h in tasks_res],
        'reviews': [{'_id': h.get('_id'), **h.get('_source', {})} for h in reviews_res],
        'failures': [{'_id': h.get('_id'), **h.get('_source', {})} for h in failures_res],
        'provenance': [{'_id': h.get('_id'), **h.get('_source', {})} for h in provenance_res],
        'repos': repos_res,
        'projects': projects_res,
        'elastro_savings': token_metrics['savings'],
        'token_metrics': token_metrics,
    }
    
    _SNAPSHOT_CACHE_DATA = result
    _SNAPSHOT_CACHE_TIME = now
    return result


# ─── Agent process control ────────────────────────────────────────────────────

# AP-7 resolved: WORKER_ENV_FILE (.env.local) removed — workers receive LLM config exclusively
# via hydrate_secrets_from_openbao() + sync_llm_env_from_workspace() (OpenBao is the
# single source of truth for all secrets). No competing local file read on startup.


def _find_worker_pids() -> dict:
    # Deprecated: Handled asynchronously heavily via agent-system-workers heartbeat schema
    return {'manager': [], 'handlers': []}


def agents_status() -> dict:
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
                    logger.debug("api_snapshot: heartbeat timestamp parse failed", exc_info=True)

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
    """
    After stopping workers, reset tasks stuck in 'running' back to their
    appropriate queued state so they can be picked up on next start.
    """
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
            # Tester and reviewer work lives in 'review', pm lives in 'planned'
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
    return {'ok': True, 'killed_pids': killed, 'requeued_tasks': requeued}


def agents_start() -> dict:
    """Start manager and worker_handlers if not already running."""
    pids = _find_worker_pids()
    started = []

    # AP-7: .env.local env overlay removed — workers receive secrets exclusively
    # from OpenBao (via hydrate_secrets_from_openbao) and the inherited process env
    # already populated by apply_runtime_config() at dashboard startup.
    env = dict(os.environ)

    # AP-6: log files removed — worker stderr goes to subprocess.DEVNULL (stdout/stderr only, 12-factor)
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
    """
    Schedule `./flume restart --all` in a detached shell so systemd can restart
    the dashboard and workers bounce. If `flume` is missing, fall back to
    stopping/starting worker processes only.
    """
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
            return {
                'ok': True,
                'mode': 'flume',
                'message': 'Restart scheduled. You may lose connection briefly; refresh if the page stops responding.',
            }
        except SAFE_EXCEPTIONS:
            logger.warning("api_agents_restart: flume CLI restart failed, falling back to workers_only", exc_info=True)
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
        return {'ok': False, 'error': str(e)[:400]}


def _github_https_clone_url(repo_url: str, gh_token: str) -> str:
    """
    Embed a GitHub PAT for non-interactive HTTPS clone.

    GitHub documents https://x-access-token:<token>@github.com/... for both classic
    and fine-grained PATs; raw https://<token>@github.com/... can fail for some tokens.
    """
    if not gh_token or not repo_url.startswith('https://github.com/'):
        return repo_url
    if '://' not in repo_url:
        return repo_url
    host_and_rest = repo_url.split('://', 1)[1]
    if '@' in host_and_rest.split('/', 1)[0]:
        return repo_url
    enc = urllib.parse.quote(gh_token, safe='')
    return re.sub(
        r'^https://github\.com/',
        f'https://x-access-token:{enc}@github.com/',
        repo_url,
    )


def maybe_auto_start_workers():
    """
    Start worker manager + handlers when the dashboard starts (same as POST /api/workflow/agents/start).

    Set FLUME_AUTO_START_WORKERS=0 (or false/no/off) to disable — e.g. if you run workers on another host.
    """
    raw = os.environ.get('FLUME_AUTO_START_WORKERS', '1').strip().lower()
    if raw in ('0', 'false', 'no', 'off'):
        return
    try:
        result = agents_start()
        started = result.get('started') or []
        if started:
            logger.info(f'Flume: auto-started workers: {started}')
        elif result.get('already_running'):
            logger.info('Flume: workers already running (skipped auto-start).')
    except SAFE_EXCEPTIONS as e:
        logger.info(f'Flume: warning — could not auto-start workers: {e}')








from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # Re-run validation natively inside the event loop in case env vars were mutated post-import
        resolve_safe_workspace()
        
        # AP-15: WORKSPACE_ROOT may be a read-only bind-mount (/local-repos:ro).
        # Only attempt mkdir when the directory doesn't already exist.
        # For remote-only deployments the mount target is /dev/null (a file, not
        # a dir), so we skip mkdir entirely and let ES be the source of truth.
        if not WORKSPACE_ROOT.exists():
            WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
        logger.info(json.dumps({
            "event": "workspace_initialized",
            "path": str(WORKSPACE_ROOT),
            "source": "FLUME_WORKSPACE" if os.environ.get('FLUME_WORKSPACE') else "fallback_home",
            "status": "success"
        }))
    except SAFE_EXCEPTIONS as e:
        logger.error(json.dumps({
            "event": "workspace_initialization_failure",
            "path": str(WORKSPACE_ROOT),
            "error": str(e),
            "status": "fatal"
        }))
        raise WorkspaceInitializationError(f"Failed to initialize workspace: {e}") from e


    # AP-3 resolved: _migrate_legacy_projects_json() removed — migration is complete.

    # Fallback LLM config seed: if the CLI's SeedLLMConfig write failed (e.g. due to
    # macOS Docker Desktop port-forward lag), seed from the container's env vars.
    # Uses doc_as_upsert so we never overwrite a value the user saved via the Settings UI.
    _seed_llm_config_from_env()

    # Ignite the child process worker swarm dynamically natively post-workspace assembly
    maybe_auto_start_workers()

    threading.Thread(target=_es_bulk_flusher_loop, daemon=True).start()

    # Start the auto-unblocker daemon. Self-contained background thread; no-op
    # when FLUME_AUTO_UNBLOCK_ENABLED=0. See src/dashboard/auto_unblock.py.
    try:
        import auto_unblock as _auto_unblock
        _auto_unblock.maybe_start(
            es_search=es_search,
            es_post=es_bulk_update_proxy,
            append_note=_lazy_append_task_agent_log_note,
        )
    except SAFE_EXCEPTIONS as _exc:
        logger.warning(f'auto_unblock.start_failed: {_exc}')

    # Start the autonomy sweeps (parent-revival + stuck-worker watchdog +
    # plan-progress scanner). See src/dashboard/autonomy_sweeps.py.
    try:
        import autonomy_sweeps as _autonomy
        _autonomy.maybe_start(
            es_search=es_search,
            es_post=es_bulk_update_proxy,
            es_upsert=es_upsert,
            append_note=_lazy_append_task_agent_log_note,
            list_projects=load_projects_registry,
            logger=logger,
        )
    except SAFE_EXCEPTIONS as _exc:
        logger.warning(f'autonomy_sweeps.start_failed: {_exc}')

    from core.elasticsearch import _get_httpx_verify
    app.state.http_client = httpx.AsyncClient(verify=_get_httpx_verify())
    yield
    await app.state.http_client.aclose()
    agents_stop()

app = FastAPI(title="Flume Enterprise API", lifespan=lifespan)

limiter = Limiter(key_func=get_remote_address, default_limits=["2000/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

cors_origins_env = os.environ.get("FLUME_CORS_ORIGINS", "")
if cors_origins_env:
    allow_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
else:
    allow_origins = ["http://localhost:8080", "http://localhost:8765", "http://127.0.0.1:8080"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

import uuid
from starlette.middleware.base import BaseHTTPMiddleware

class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        start_time = time.time()
        response = await call_next(request)
        process_time = (time.time() - start_time) * 1000
        
        # Avoid logging noisy polling or health checks at INFO level
        is_noisy = "/health" in request.url.path or "/tasks" in request.url.path
        log_func = logger.debug if is_noisy else logger.info
        
        log_func(
            f"{request.method} {request.url.path} - {response.status_code}",
            extra={
                "structured_data": {
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(process_time, 2),
                    "request_id": req_id
                }
            }
        )
        response.headers["X-Request-ID"] = req_id
        return response

app.add_middleware(LoggingMiddleware)

# The legacy @app.on_event("startup") was migrated strictly up to the FastAPI lifespan architecture above.







def _parse_float_env(key: str, default: float) -> float:
    val_str = os.environ.get(key)
    if val_str is None:
        return default
    try:
        val_flt = float(val_str)
        if val_flt > 0:
            return val_flt
        logger.warning(
            f"Invalid {key} value (must be > 0). Falling back to default.",
            extra={
                "component": "config_parser",
                "invalid_value": val_flt,
                "default_value": default,
            }
        )
    except (ValueError, TypeError):
        logger.warning(
            f"Invalid {key} format. Falling back to default.",
            extra={
                "component": "config_parser",
                "invalid_value": val_str,
                "default_value": default,
            }
        )
    return default










# --- Mount Domain Routers ---
from api.projects import router as projects_router
from api.repos import router as repos_router
from api.intake import router as intake_router
from api.tasks import router as tasks_router
from api.settings import router as settings_router
from api.workflow import router as workflow_router
from api.nodes import router as nodes_router
from api.security import router as security_router
from api.system import router as system_router

app.include_router(projects_router)
app.include_router(repos_router)
app.include_router(intake_router)
app.include_router(tasks_router)
app.include_router(settings_router)
app.include_router(workflow_router)
app.include_router(nodes_router)
app.include_router(security_router)
app.include_router(system_router)

# Static Mount for Frontend
from fastapi.responses import FileResponse

if STATIC_ROOT.exists():
    asset_dir = STATIC_ROOT / "assets"
    if asset_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(asset_dir)), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa_catchall(full_path: str):
        if full_path.startswith("api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        path = STATIC_ROOT / full_path
        if path.is_file():
            return FileResponse(path)
        return FileResponse(STATIC_ROOT / "index.html")
else:
    @app.get("/{full_path:path}")
    def fallback_root(full_path: str):
        return {"status": "ok", "message": "Flume UI bundle missing. CI fallback active."}

if __name__ == "__main__":
    import uvicorn
    import os
    json_mode = os.environ.get('FLUME_JSON_LOGS', 'false').lower() == 'true'
    formatter = "utils.logger.JSONFormatter" if json_mode else "utils.logger.ConsoleFormatter"
    
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"] = {"()": formatter}
    log_config["formatters"]["default"] = {"()": formatter}
    
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_config=log_config)
    except WorkspaceInitializationError as e:
        logger.error(json.dumps({
            "event": "workspace_initialization_fatal",
            "error": str(e),
            "status": "fatal"
        }))
        import sys
        sys.exit(1)
