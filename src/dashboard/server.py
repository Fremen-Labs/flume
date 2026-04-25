#!/usr/bin/env python3
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
import uuid
import asyncio
import ssl
import hvac
import httpx



import subprocess
import urllib.request
import urllib.parse
from urllib.parse import urlparse
from utils.url_helpers import is_remote_url
from utils.async_subprocess import run_cmd_async
from api.models import (
    BulkUpdateRequest,
    LogLevelRequest,
    ClientLogRequest,
    LLMSettingsRequest,
    LLMCredentialsActionRequest,
    RepoSettingsRequest,
    AgentModelsRequest,
)
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pydantic import BaseModel
import traceback
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

from utils.logger import get_logger, set_global_log_level
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
_startup_logger = _get_startup_logger('es_bootstrap')
try:
    import urllib.request as _ur
    import urllib.error as _ue
    _es_check_url = os.environ.get('ES_URL', 'http://elasticsearch:9200' if os.environ.get('FLUME_NATIVE_MODE') != '1' else 'http://localhost:9200')
    _check_req = _ur.Request(f"{_es_check_url}/agent-task-records", method='HEAD')
    with _ur.urlopen(_check_req, timeout=3) as _r:
        if _r.status == 200:
            _startup_logger.info("ES index verification passed — agent-task-records exists")
except _ue.HTTPError as _e:
    if _e.code == 404:
        _startup_logger.warning("ES index 'agent-task-records' not found — was `flume start` used to boot?")
except Exception as _e:
    _startup_logger.warning(f"ES index verification skipped — cannot reach Elasticsearch: {_e}")


_DEFAULT_ES = 'http://localhost:9200' if os.environ.get('FLUME_NATIVE_MODE') == '1' else 'http://elasticsearch:9200'
ES_URL = os.environ.get('ES_URL', _DEFAULT_ES).rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY', '')
ES_VERIFY_TLS = os.environ.get('ES_VERIFY_TLS', 'false').lower() == 'true'


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
    model = os.environ.get('LLM_MODEL', '').strip()
    provider = os.environ.get('LLM_PROVIDER', '').strip()
    base_url = os.environ.get('LLM_BASE_URL', '').strip()

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
        es_url_local = OS_ENV_ES_URL = os.environ.get('ES_URL', _DEFAULT_ES).rstrip('/')
        url = f'{es_url_local}/flume-llm-config/_update/singleton'
        headers: dict = {'Content-Type': 'application/json'}
        api_key = os.environ.get('ES_API_KEY', '')
        if api_key and 'bypass' not in api_key:
            headers['Authorization'] = f'ApiKey {api_key}'

        # Fetch first to check if values already set — never clobber user changes
        get_req = urllib.request.Request(
            f'{es_url_local}/flume-llm-config/_doc/singleton',
            headers=headers, method='GET',
        )
        try:
            with urllib.request.urlopen(get_req, timeout=5) as r:
                import json as _json
                existing_src = _json.loads(r.read()).get('_source', {})
                # Remove fields already present in ES so we don't overwrite them
                for k in list(doc.keys()):
                    if existing_src.get(k):
                        doc.pop(k, None)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                logger.warning(f'_seed_llm_config_from_env: GET failed ({e}) — proceeding with full upsert')
        except Exception as e:
            logger.warning(f'_seed_llm_config_from_env: GET error ({e}) — proceeding with full upsert')

        if not doc:
            logger.info('_seed_llm_config_from_env: all LLM fields already present in ES — nothing to seed')
            return

        body = json.dumps({'doc': doc, 'doc_as_upsert': True}).encode()
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=5) as r:
            logger.info(f'_seed_llm_config_from_env: seeded {list(doc.keys())} → flume-llm-config (model={model})')
    except Exception as e:
        logger.warning(f'_seed_llm_config_from_env: non-fatal failure — {e}')

HOST = os.environ.get('DASHBOARD_HOST', '0.0.0.0')
PORT = int(os.environ.get('DASHBOARD_PORT', '8765'))
# Pre-built Vite output only — editing src/frontend/src/*.tsx requires: ./flume build-ui (see install/README.md).
STATIC_ROOT = Path(__file__).resolve().parent.parent / 'frontend' / 'dist'

from utils.workspace import resolve_safe_workspace, WorkspaceInitializationError

# Module-level paths are bounded to block AppSec Path Traversals seamlessly isolating the host
WORKSPACE_ROOT = resolve_safe_workspace()
from config import AppConfig, get_settings  # type: ignore

# AP-2 resolved: WORKER_STATE removed — worker lifecycle state belongs in ES (flume-workers index).
# AP-9 resolved: SESSIONS_DIR removed — plan sessions already fully migrated to agent-plan-sessions ES index.
# AP-3 resolved: PROJECTS_REGISTRY removed — projects.json migration is complete; sentinel logic deleted.

LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'http://localhost:11434')
LLM_MODEL = os.environ.get('LLM_MODEL', 'llama3.2')

# AP-1: Sequence counters are now stored atomically in the ES `flume-counters` index.
# One document per prefix (e.g. 'task', 'epic'); field `value` = highest allocated N.
# See es_counter_increment() and es_counter_hwm() below.
COUNTERS_INDEX = 'flume-counters'

ctx = None
if not ES_VERIFY_TLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE



from core.projects_store import (
    load_projects_registry,
)


from core.elasticsearch import (
    es_search,
    find_task_doc_by_logical_id,
    es_upsert,
    es_post,
    es_bulk_update_proxy,
    _es_bulk_flusher_loop,
    es_delete_doc,
)

from core.sessions_store import _utcnow_iso

def _lazy_append_task_agent_log_note(es_id: str, note: str) -> bool:
    from api.tasks import _append_task_agent_log_note
    return _append_task_agent_log_note(es_id, note)


def _sync_llm_runtime_env():
    try:
        from workspace_llm_env import sync_llm_env_from_workspace  # type: ignore

        sync_llm_env_from_workspace(WORKSPACE_ROOT)
    except Exception:
        logger.debug("sync_llm_env_from_workspace: failed on startup (non-critical)", exc_info=True)

# --- Extracted Domain: Planning ---

# --- Extracted Domain: Tasks ---
from core.tasks import (
    delete_task_branches, load_workers,
    git_repo_info, resolve_default_branch
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
            except Exception:
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
                except Exception:
                    logger.debug("api_snapshot: heartbeat timestamp parse failed", exc_info=True)

        return {
            'running': active_nodes > 0 and status != 'paused',
            'manager_running': active_nodes > 0,
            'handlers_running': active_nodes > 0,
            'manager_pids': [],
            'handler_pids': [],
            'cluster_status': status
        }
    except Exception as e:
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
    except Exception:
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
            except Exception:
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
        except Exception:
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
    except Exception as e:
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
    except Exception as e:
        logger.info(f'Flume: warning — could not auto-start workers: {e}')








from fastapi import FastAPI, WebSocket, Request, Depends, HTTPException, Header
from fastapi.responses import JSONResponse
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
    except Exception as e:
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
            logger=logger,
        )
    except Exception as _exc:
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
    except Exception as _exc:
        logger.warning(f'autonomy_sweeps.start_failed: {_exc}')

    app.state.http_client = httpx.AsyncClient()
    yield
    await app.state.http_client.aclose()
    agents_stop()

app = FastAPI(title="Flume Enterprise API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from starlette.middleware.base import BaseHTTPMiddleware

class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
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
                    "duration_ms": round(process_time, 2)
                }
            }
        )
        return response

app.add_middleware(LoggingMiddleware)

# The legacy @app.on_event("startup") was migrated strictly up to the FastAPI lifespan architecture above.






from urllib.parse import urlunparse

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

@app.post('/api/tasks/bulk-update')
async def api_tasks_bulk_update(payload: BulkUpdateRequest):
    """
    Bulk archive or delete tasks from the project task list.

    Body: { "ids": ["task-1", ...], "action": "archive" | "delete", "repo": "<project id>" }
    When `repo` is set, tasks whose `repo` field does not match are skipped (failed).
    """
    _MAX_BULK = 200
    ids = payload.ids or []
    action = (payload.action or '').strip().lower()
    repo = (payload.repo or '').strip()

    if action not in ('archive', 'delete'):
        return JSONResponse(
            status_code=400,
            content={'error': f'action must be "archive" or "delete", got {action!r}'},
        )
    if not isinstance(ids, list):
        return JSONResponse(status_code=400, content={'error': 'ids must be a list'})
    if not ids:
        return JSONResponse(status_code=400, content={'error': 'ids must not be empty'})
    if len(ids) > _MAX_BULK:
        return JSONResponse(
            status_code=400,
            content={'error': f'bulk limit is {_MAX_BULK} tasks per call'},
        )

    str_ids = [str(i) for i in ids]
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    ok, failed = [], []

    if action == 'archive':
        for task_id in str_ids:
            try:
                es_id, src = find_task_doc_by_logical_id(task_id)
                if not es_id or src is None:
                    failed.append({'task_id': task_id, 'error': 'not found'})
                    continue
                logical_repo = (src.get('repo') or '').strip()
                if repo and logical_repo != repo:
                    failed.append({'task_id': task_id, 'error': 'repo mismatch'})
                    continue
                doc = {
                    'status': 'archived',
                    'active_worker': None,
                    'needs_human': False,
                    'updated_at': now,
                    'last_update': now,
                }
                es_post(f'agent-task-records/_update/{es_id}', {'doc': doc})
                ok.append({'task_id': task_id})
            except Exception as exc:
                logger.error(f'bulk-update archive: task {task_id} failed: {exc}')
                failed.append({'task_id': task_id, 'error': str(exc)[:200]})
        logger.info(f'bulk-update archive: ok={len(ok)} failed={len(failed)}')
        return {'archived': ok, 'failed': failed}

    # delete — clean up git branches while ES rows still exist, then remove docs
    try:
        await delete_task_branches(str_ids, repo)
    except Exception as exc:
        logger.warning(f'bulk-update delete: delete_task_branches: {exc}')

    for task_id in str_ids:
        try:
            es_id, src = find_task_doc_by_logical_id(task_id)
            if not es_id or src is None:
                failed.append({'task_id': task_id, 'error': 'not found'})
                continue
            logical_repo = (src.get('repo') or '').strip()
            if repo and logical_repo != repo:
                failed.append({'task_id': task_id, 'error': 'repo mismatch'})
                continue
            if es_delete_doc('agent-task-records', es_id):
                ok.append({'task_id': task_id})
            else:
                failed.append({'task_id': task_id, 'error': 'not found in index'})
        except Exception as exc:
            logger.error(f'bulk-update delete: task {task_id} failed: {exc}')
            failed.append({'task_id': task_id, 'error': str(exc)[:200]})

    logger.info(f'bulk-update delete: deleted={len(ok)} failed={len(failed)}')
    return {'deleted': ok, 'failed': failed}





@app.get("/api/repos/{project_id}/branches")
async def api_repo_branches(project_id: str):
    """
    Return git branches for a project.

    AP-4B: For remote repos (clone_status in ['indexed', 'cloned']) this calls
    the GitHostClient REST API and requires no local clone.
    For locally-mounted repos (clone_status='local') the original git subprocess
    path is used.

    When the repo is not yet available (still cloning/indexing), the response
    includes `cloneStatus` so the frontend can start the polling loop without
    an extra round-trip to /clone-status.
    """
    from utils.git_host_client import get_git_client, GitHostAuthError, GitHostError  # noqa

    registry = load_projects_registry()
    proj = next((p for p in registry if p.get("id") == project_id), None)
    if not proj:
        return JSONResponse(status_code=404, content={"error": f"Project '{project_id}' not found"})

    cs = proj.get("clone_status", "no_repo")
    clone_error = proj.get("clone_error")

    # In-flight states: clone/ingest still running — send polling hint to UI
    if cs in ("cloning", "indexing", "pending"):
        return {
            "gitAvailable": False,
            "cloneStatus": cs,
            "cloneError": None,
            "branches": [],
            "message": "Repository is being cloned in the background\u2026",
        }

    # ── Local clone precedence check ──────────────────────────────────────────
    _local_path = proj.get("path") or ''
    has_local_clone = _local_path and Path(_local_path).joinpath('.git').exists()

    # ── Remote repo path: use GitHostClient REST API (no local clone available) ──
    repo_url = proj.get("repoUrl") or ""
    if not has_local_clone and cs in ("indexed", "cloned") and repo_url and is_remote_url(repo_url):
        try:
            client = get_git_client(proj)
            branches = client.get_branches()
            default  = client.get_default_branch()
            return {"gitAvailable": True, "branches": branches, "default": default}
        except GitHostAuthError as e:
            return JSONResponse(status_code=401, content={
                "gitAvailable": False,
                "error": "No credentials configured. Add a PAT in Settings \u2192 Repositories.",
                "detail": str(e)[:200],
            })
        except GitHostError as e:
            return JSONResponse(status_code=500, content={"error": str(e)[:300]})
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)[:300]})

    # ── Local repo path: original git subprocess (clone_status='local') ─────────
    # AP-12: Explicit guard — no silent WORKSPACE_ROOT fallback for non-local projects.
    _local_path = proj.get("path") or ''
    if not _local_path:
        return JSONResponse(status_code=400, content={"error": "No local repo path available. This project has no local clone."})
    repo_path = Path(_local_path)

    if not (repo_path / ".git").exists():
        if cs == "failed":
            message = f"Clone failed: {clone_error or 'Unknown error'}"
        else:
            message = (
                "This project is not a Git repository. "
                'Add one by creating the project with a clone URL or run "git init" in the project folder.'
            )
        return {
            "gitAvailable": False,
            "cloneStatus": cs,
            "cloneError": clone_error,
            "branches": [],
            "message": message,
        }

    try:
        rc, raw, err = await run_cmd_async(
            "git", "-C", str(repo_path), "branch", "-a", "--format=%(refname:short)",
            timeout=10,
        )
        if rc == 128:
            return JSONResponse(status_code=500, content={"error": "git branch exited 128: Repository refs may be corrupt."})
        if rc != 0:
            return JSONResponse(status_code=500, content={"error": f"git branch failed: {err}"})
        all_branches = [b.strip() for b in raw.splitlines() if b.strip()]
        seen: set = set()
        branches: list = []
        for b in all_branches:
            name = b.removeprefix("origin/") if b.startswith("origin/") else b
            if name and name != "HEAD" and name not in seen:
                seen.add(name)
                branches.append(name)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": f"git branch failed: {exc}"})

    default = await resolve_default_branch(
        repo_path, override=proj.get("gitflow", {}).get("defaultBranch")
    )
    return {"gitAvailable": True, "branches": branches, "default": default}


@app.get("/api/repos/{project_id}/tree")
async def api_repo_tree(project_id: str, branch: str = ""):
    """
    Return a flat list of all git-tracked files/dirs for a given branch.
    AP-4B: Uses GitHostClient REST API for remote repos (no local clone required).
    """
    from utils.git_host_client import get_git_client, GitHostAuthError, GitHostError  # noqa

    registry = load_projects_registry()
    proj = next((p for p in registry if p.get("id") == project_id), None)
    if not proj:
        return JSONResponse(status_code=404, content={"error": f"Project '{project_id}' not found"})

    cs = proj.get("clone_status", "no_repo")
    repo_url = proj.get("repoUrl") or ""

    if cs in ("cloning", "indexing", "pending"):
        return JSONResponse(status_code=400, content={"error": "Repository is currently being cloned."})

    # ── Local clone precedence check ──────────────────────────────────────────
    _local_path = proj.get("path") or ''
    has_local_clone = _local_path and Path(_local_path).joinpath('.git').exists()

    # ── Remote repo: GitHostClient REST API ──────────────────────────────────
    if not has_local_clone and cs in ("indexed", "cloned") and repo_url and is_remote_url(repo_url):
        try:
            client = get_git_client(proj)
            if not branch:
                branch = client.get_default_branch()
            entries = client.get_tree(branch=branch)
            return {"branch": branch, "entries": entries}
        except GitHostAuthError as e:
            return JSONResponse(status_code=401, content={
                "error": "No credentials — add a PAT in Settings \u2192 Repositories.",
                "detail": str(e)[:200],
            })
        except GitHostError as e:
            return JSONResponse(status_code=500, content={"error": str(e)[:300]})

    # ── Local repo: git subprocess ────────────────────────────────────────────
    # AP-12: Explicit guard — no silent WORKSPACE_ROOT fallback.
    _local_path = proj.get("path") or ''
    if not _local_path:
        return JSONResponse(status_code=400, content={"error": "No local repo path. This project has no local clone."})
    repo_path = Path(_local_path)
    if not (repo_path / ".git").exists():
        return JSONResponse(status_code=400, content={"error": "Not a git repository"})

    if not branch:
        branch = await resolve_default_branch(
            repo_path, override=proj.get("gitflow", {}).get("defaultBranch")
        )

    try:
        rc, raw, err = await run_cmd_async(
            "git", "-C", str(repo_path), "ls-tree", "-r", "--long", "--full-tree", branch,
            timeout=30,
        )
        if rc != 0:
            return JSONResponse(status_code=400, content={"error": f"Could not read tree for branch '{branch}': {err}"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": f"git ls-tree failed: {exc}"})

    entries = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) < 2:
            continue
        meta, size_and_path = parts[0], parts[1] if len(parts) == 2 else parts[1]
        meta_parts = meta.split()
        if len(meta_parts) < 3:
            continue
        obj_type = meta_parts[1]
        if len(parts) == 3:
            size = parts[1].strip()
            file_path = parts[2].strip()
        else:
            size = "-"
            file_path = parts[1].strip()
        entries.append({"path": file_path, "type": "blob" if obj_type == "blob" else "tree", "size": size})

    dirs_seen: set = set()
    dir_entries = []
    for e in entries:
        parts_path = e["path"].split("/")
        for depth in range(1, len(parts_path)):
            dir_path = "/".join(parts_path[:depth])
            if dir_path not in dirs_seen:
                dirs_seen.add(dir_path)
                dir_entries.append({"path": dir_path, "type": "tree", "size": "-"})

    return {"branch": branch, "entries": entries + dir_entries}


@app.get("/api/repos/{project_id}/file")
async def api_repo_file(project_id: str, path: str = "", branch: str = ""):
    """
    Return the content of a single file from the git tree.
    AP-4B: Uses GitHostClient REST API for remote repos (no local clone required).
    """
    from utils.git_host_client import get_git_client, GitHostAuthError, GitHostNotFoundError, GitHostError  # noqa

    if not path:
        return JSONResponse(status_code=400, content={"error": "path is required"})

    registry = load_projects_registry()
    proj = next((p for p in registry if p.get("id") == project_id), None)
    if not proj:
        return JSONResponse(status_code=404, content={"error": f"Project '{project_id}' not found"})

    cs = proj.get("clone_status", "no_repo")
    repo_url = proj.get("repoUrl") or ""

    # Sanitise path — prevent directory traversal
    clean_path = path.lstrip("/")
    if ".." in clean_path.split("/"):
        return JSONResponse(status_code=400, content={"error": "Invalid path"})

    # ── Local clone precedence check ──────────────────────────────────────────
    _local_path = proj.get("path") or ''
    has_local_clone = _local_path and Path(_local_path).joinpath('.git').exists()

    # ── Remote repo: GitHostClient REST API ──────────────────────────────────
    if not has_local_clone and cs in ("indexed", "cloned") and repo_url and is_remote_url(repo_url):
        try:
            client = get_git_client(proj)
            if not branch:
                branch = client.get_default_branch()
            content_bytes = client.get_file(clean_path, branch=branch)
            return _make_file_response(content_bytes, clean_path)
        except GitHostAuthError as e:
            return JSONResponse(status_code=401, content={
                "error": "No credentials — add a PAT in Settings \u2192 Repositories.",
                "detail": str(e)[:200],
            })
        except GitHostNotFoundError:
            return JSONResponse(status_code=404, content={"error": f"File '{clean_path}' not found on branch '{branch}'"})
        except GitHostError as e:
            return JSONResponse(status_code=500, content={"error": str(e)[:300]})

    # ── Local repo: git subprocess ────────────────────────────────────────────
    # AP-12: Explicit guard — no silent WORKSPACE_ROOT fallback.
    _local_path = proj.get("path") or ''
    if not _local_path:
        return JSONResponse(status_code=400, content={"error": "No local repo path. This project has no local clone."})
    repo_path = Path(_local_path)
    if not (repo_path / ".git").exists():
        return JSONResponse(status_code=400, content={"error": "Not a git repository"})

    if not branch:
        branch = await resolve_default_branch(
            repo_path, override=proj.get("gitflow", {}).get("defaultBranch")
        )

    try:
        rc, out, err = await run_cmd_async(
            "git", "-C", str(repo_path), "show", f"{branch}:{clean_path}",
            timeout=15,
        )
        if rc != 0:
            if "does not exist" in err or "exists on disk" in err or rc == 128:
                return JSONResponse(status_code=404, content={"error": f"File '{clean_path}' not found on branch '{branch}'"})
            return JSONResponse(status_code=500, content={"error": f"git show failed: {err}"})
        content_bytes = out.encode("utf-8")
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": f"git show failed: {exc}"})

    # Detect binary by sniffing for null bytes in the first 8KB
    sample = content_bytes[:8192]
    is_binary = b"\x00" in sample
    if is_binary:
        return {"binary": True, "content": None, "size": len(content_bytes)}

    return {
        "binary": False,
        "content": content_bytes.decode("utf-8", errors="replace"),
        "size": len(content_bytes),
    }


def _make_file_response(content_bytes: bytes, path: str) -> dict:
    """Shared file response formatter for both git subprocess and GitHostClient paths."""
    sample = content_bytes[:8192]
    is_binary = b"\x00" in sample
    if is_binary:
        return {"binary": True, "content": None, "size": len(content_bytes)}
    return {
        "binary": False,
        "content": content_bytes.decode("utf-8", errors="replace"),
        "size": len(content_bytes),
    }


@app.get("/api/repos/{project_id}/diff")
async def api_repo_diff(project_id: str, base: str = "", head: str = ""):
    """Return a unified diff between two branches for a project."""
    if not base or not head:
        return JSONResponse(status_code=400, content={"error": "base and head branch parameters are required"})

    registry = load_projects_registry()
    proj = next((p for p in registry if p.get("id") == project_id), None)
    if not proj:
        return JSONResponse(status_code=404, content={"error": f"Project '{project_id}' not found"})

    # AP-12: Explicit guard — no silent WORKSPACE_ROOT fallback.
    _local_path = proj.get("path") or ''
    if not _local_path:
        return JSONResponse(status_code=400, content={"error": "No local repo path. Diff requires a locally-mounted repo."})
    repo_path = Path(_local_path)
    if not (repo_path / ".git").exists():
        return JSONResponse(status_code=400, content={"error": "Not a git repository"})

    if base == head:
        return {"base": base, "head": head, "files": [], "diff": "", "truncated": False, "identical": True}

    MAX_DIFF_LINES = 3000
    ref = f"{base}...{head}"

    # Best-effort fetch (non-blocking)
    try:
        await run_cmd_async("git", "-C", str(repo_path), "fetch", "origin", "--quiet", timeout=10)
    except Exception as _e:
        logger.debug("api_repo_diff: fetch failed (best-effort)", exc_info=True)

    files = []
    try:
        rc, stat_raw, stat_err = await run_cmd_async(
            "git", "-C", str(repo_path), "diff", "--stat", "--stat-width=1000", ref,
            timeout=15,
        )
        if rc == 0:
            for line in stat_raw.splitlines():
                parts = line.strip().split("|")
                if len(parts) != 2:
                    continue
                path_part = parts[0].strip()
                change_part = parts[1].strip()
                if not path_part or path_part.startswith("changed"):
                    continue
                ins = sum(1 for c in change_part if c == "+")
                dels = sum(1 for c in change_part if c == "-")
                files.append({"path": path_part, "insertions": ins, "deletions": dels, "status": "modified"})
    except Exception as _e:
        logger.debug("api_repo_diff: diff --stat failed (best-effort)", exc_info=True)

    diff_text = ""
    truncated = False
    try:
        rc, raw_diff, diff_err = await run_cmd_async(
            "git", "-C", str(repo_path), "diff", ref,
            timeout=30,
        )
        if rc == 0:
            diff_lines = raw_diff.splitlines()
            if len(diff_lines) > MAX_DIFF_LINES:
                diff_text = "\n".join(diff_lines[:MAX_DIFF_LINES])
                truncated = True
            else:
                diff_text = raw_diff
    except Exception as _e:
        logger.debug("api_repo_diff: git diff failed (best-effort)", exc_info=True)

    identical = not diff_text.strip() and not files
    return {
        "base": base, "head": head,
        "files": files, "diff": diff_text,
        "truncated": truncated, "identical": identical,
    }



# --- Mount Domain Routers ---
from api.projects import router as projects_router
app.include_router(projects_router)
from api.intake import router as intake_router
app.include_router(intake_router)
from api.tasks import router as tasks_router
app.include_router(tasks_router)
from api.settings import router as settings_router
app.include_router(settings_router)
from api.workflow import router as workflow_router
app.include_router(workflow_router)
from api.nodes import router as nodes_router
app.include_router(nodes_router)
from api.security import router as security_router
app.include_router(security_router)
from api.system import router as system_router
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
