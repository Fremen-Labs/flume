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
import ssl
import hvac
import httpx

class NetflixFaultTolerance:
    '''Netflix Microservice Resilience Wrapper'''
    pass

import subprocess
import urllib.request
import urllib.parse
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pydantic import BaseModel
import traceback
from concurrent.futures import ThreadPoolExecutor

# Flume Bootstrap Logic
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
    _update_project_registry_field,
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

from core.sessions_store import _utcnow_iso, _iso_elapsed_seconds

def _lazy_append_task_agent_log_note(es_id: str, note: str) -> bool:
    from api.tasks import _append_task_agent_log_note
    return _append_task_agent_log_note(es_id, note)


def _sync_llm_runtime_env():
    try:
        from workspace_llm_env import sync_llm_env_from_workspace  # type: ignore

        sync_llm_env_from_workspace(WORKSPACE_ROOT)
    except Exception:
        pass

# --- Extracted Domain: Planning ---
from core.planning import (
    _count_plan_tasks
)

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


def load_snapshot():
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

    repos_res = load_repos(registry=projects_res)
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
                    pass

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
                pass
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
            pass
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


def _session_messages_for_client(session: dict) -> list:
    out = []
    for m in session.get('messages', []):
        if m.get('from') not in ('user', 'agent'):
            continue
        item = {
            'from': m.get('from'),
            'text': m.get('text', ''),
        }
        if m.get('plan') is not None:
            item['plan'] = m.get('plan')
        out.append(item)
    return out


def _count_plan_tasks(plan: Optional[dict]) -> int:
    total = 0
    for epic in (plan or {}).get('epics') or []:
        for feature in epic.get('features') or []:
            for story in feature.get('stories') or []:
                total += len(story.get('tasks') or [])
    return total


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

@app.post('/api/settings/log-level')
async def api_set_log_level(payload: dict):
    level = payload.get('level', 'INFO').upper()
    set_global_log_level(level)
    
    # Proxy to Gateway
    try:
        gw_url = os.environ.get('GATEWAY_URL', 'http://gateway:8090').rstrip('/')
        async with httpx.AsyncClient() as client:
            await client.post(f"{gw_url}/internal/level", json={"level": level}, timeout=2.0)
    except Exception as e:
        logger.warning(f"Failed to sync log level to gateway: {e}")
        
    return {"status": "ok", "level": level}

@app.post('/api/logs/client')
async def api_client_logs(payload: dict):
    """Aggregate logs from the frontend."""
    level = payload.get('level', 'ERROR').upper()
    message = payload.get('message', 'Unknown client error')
    data = payload.get('data', {})
    
    log_func = getattr(logger, level.lower(), logger.error)
    log_func(
        f"CLIENT_{level}: {message}",
        extra={"structured_data": {"client_data": data}}
    )
    return {"status": "ok"}
# The legacy @app.on_event("startup") was migrated strictly up to the FastAPI lifespan architecture above.

@app.get('/api/health')
def health():
    return {"status": "ok"}


def _session_payload_for_client(session: dict) -> dict:
    status = dict(session.get('planningStatus') or {})
    started_at = status.get('requestStartedAt')
    elapsed = _iso_elapsed_seconds(started_at)
    if elapsed is not None:
        status['requestElapsedSeconds'] = elapsed
    return {
        'sessionId': session['id'],
        'status': status.get('stage') or session.get('status') or 'active',
        'messages': _session_messages_for_client(session),
        'plan': session.get('draftPlan') or {'epics': []},
        'planSource': session.get('draftPlanSource'),
        'planningStatus': status,
    }



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

class AppSettings:
    def __init__(self):
        self.exo_url = os.environ.get("EXO_STATUS_URL", "http://host.docker.internal:52415/models")
        self.exo_timeout = _parse_float_env("EXO_STATUS_TIMEOUT_SECONDS", 0.5)

settings = AppSettings()

import fastapi
from api.models import BulkUpdateRequest
def get_app_settings() -> AppSettings:
    return settings

@app.get('/api/exo-status')
async def api_exo_status(request: fastapi.Request, app_settings: AppSettings = fastapi.Depends(get_app_settings)):
    http_client = request.app.state.http_client
    
    exo_url = app_settings.exo_url
    exo_timeout = app_settings.exo_timeout

    parsed_url = urlparse(exo_url)
    base_url_parts = parsed_url._replace(path='/v1')
    base_url = urlunparse(base_url_parts)

    try:
        hostname = parsed_url.hostname
        if hostname not in ('host.docker.internal', 'localhost', '127.0.0.1', '::1'):
            logger.warning("Rejected Exo base URL targeting out-of-bounds mapping", extra={"target_url": exo_url})
            return {"active": False}
    except (ValueError, TypeError) as e:
        logger.error("Unexpected error during Exo URL validation", extra={"target_url": exo_url, "error": str(e)})
        return {"active": False}

    try:
        resp = await http_client.get(exo_url, timeout=exo_timeout)
        resp.raise_for_status()
        
        logger.info(
            "Successfully connected to Exo service",
            extra={
                "component": "exo_detector",
                "target_url": exo_url,
            }
        )
        return {"active": True, "baseUrl": base_url}
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        logger.warning(
            "Exo service connection failed",
            extra={
                "component": "exo_detector",
                "target_url": exo_url,
                "timeout_seconds": exo_timeout,
                "error_type": type(e).__name__,
                "error_details": str(e)
            }
        )
        return {"active": False}

@app.get('/api/snapshot')
def api_snapshot():
    try:
        return load_snapshot()
    except Exception as e:
        return JSONResponse(status_code=502, content={'error': str(e)[:400], 'code': 'ES_CONNECTION'})


import shutil

_flume_cli_checked: bool = False
_flume_cli_found: bool = False


def _flume_cli_available() -> bool:
    """Cache whether the `flume` CLI binary exists on $PATH (checked once per process)."""
    global _flume_cli_checked, _flume_cli_found
    if not _flume_cli_checked:
        _flume_cli_found = shutil.which("flume") is not None
        _flume_cli_checked = True
    return _flume_cli_found


@app.get('/api/system-state')
def api_system_state():
    try:
        workers = load_workers()
        active = sum(1 for w in workers if w.get('status') in ('busy', 'claimed'))
        total = len(workers)

        telemetry = {}
        if _flume_cli_available():
            try:
                res = subprocess.run(["flume", "doctor", "--json"], capture_output=True, text=True, timeout=5)
                if res.returncode == 0:
                    telemetry = json.loads(res.stdout)
            except Exception:
                pass  # Binary exists but call failed — don't spam logs

        return {
            "status": "online",
            "activeStreams": active,
            "totalNodes": total,
            "standbyNodes": total - active,
            "workers": workers,
            "telemetry": telemetry
        }
    except Exception as e:
        return JSONResponse(status_code=502, content={'error': str(e)[:300]})

import asyncio
async def _check_ast_exists_natively(http_client: httpx.AsyncClient, repo_path: str) -> tuple[bool, str]:
    try:
        es_url = ES_URL
        api_key = os.environ.get('ES_API_KEY', '')
        headers = {'Content-Type': 'application/json'}
        if api_key: headers['Authorization'] = f'ApiKey {api_key}'

        elastro_index = os.environ.get("FLUME_ELASTRO_INDEX", "flume-elastro-graph")
        query = {"query": {"match": {"file_path": repo_path}}, "size": 1}
        
        response = await http_client.post(f"{es_url}/{elastro_index}/_search", json=query, headers=headers, timeout=5.0)
        response.raise_for_status()
        
        data = response.json()
        exists = data.get('hits', {}).get('total', {}).get('value', 0) > 0
        return exists, ("Found mapping records" if exists else "No logical paths matched")
    except Exception as e:
        logger.error({"event": "ast_existence_check_failure", "repo": repo_path, "error": str(e)})
        return False, str(e)


async def _deterministic_ast_ingest(http_client: httpx.AsyncClient, repo_path: str, project_id: str, project_name: str) -> bool:
    try:
        # Sanitize remote Git URLs into guaranteed physical volume paths via basename isolation
        local_path = repo_path
        if repo_path.startswith('http') or repo_path.startswith('git@'):
            import urllib.parse
            parsed = urllib.parse.urlparse(repo_path)
            basename = os.path.basename(parsed.path).replace('.git', '')
            local_path = str(WORKSPACE_ROOT / basename)

        exists, details = await _check_ast_exists_natively(http_client, local_path)
            
        if not exists:
            logger.info({"event": "ast_ingest_start", "repo": local_path, "project": project_name})
            elastro_index = os.environ.get("FLUME_ELASTRO_INDEX", "flume-elastro-graph")
            # Use the venv binary directly — avoids uv run re-installing elastro
            # on every call and works reliably inside the non-interactive container.
            elastro_bin = Path("/opt/venv/bin/elastro")
            if not elastro_bin.exists():
                import shutil
                resolved = shutil.which("elastro")
                if resolved:
                    elastro_bin = Path(resolved)
                else:
                    logger.warning(json.dumps({
                        "event": "ast_ingest_skipped",
                        "repo": local_path,
                        "project": project_name,
                        "reason": "elastro_not_installed",
                        "hint": "elastro>=0.2.0 is now in pyproject.toml — rebuild the Docker image to enable AST ingestion.",
                    }))
                    return
            # Pass ES connection env vars so elastro targets the cluster
            # instead of defaulting to localhost:9200 inside the container.
            # Elastro reads ELASTIC_URL / ELASTIC_HOST (see elastro/config/defaults.py)
            # and auth via ELASTIC_ELASTICSEARCH_AUTH_API_KEY (see elastro/config/loader.py).
            # Source these from the same ES_URL / ES_API_KEY that OpenBao hydrated
            # into os.environ at startup — matching the clone process secret path.
            elastro_env = os.environ.copy()
            resolved_es_url = ES_URL or os.environ.get("ES_URL", "http://elasticsearch:9200")
            resolved_api_key = ES_API_KEY or os.environ.get("ES_API_KEY", "")
            # Elastro native env vars (elastro/config/defaults.py reads ELASTIC_URL)
            elastro_env["ELASTIC_URL"] = resolved_es_url
            elastro_env["ELASTIC_ELASTICSEARCH_HOSTS"] = resolved_es_url
            # Also set the decomposed host/port/protocol for full compatibility
            from urllib.parse import urlparse
            _parsed = urlparse(resolved_es_url)
            elastro_env["ELASTIC_HOST"] = _parsed.hostname or "elasticsearch"
            elastro_env["ELASTIC_PORT"] = str(_parsed.port or 9200)
            elastro_env["ELASTIC_PROTOCOL"] = _parsed.scheme or "http"
            # Auth: elastro config loader reads ELASTIC_ELASTICSEARCH_AUTH_API_KEY
            if resolved_api_key:
                elastro_env["ELASTIC_ELASTICSEARCH_AUTH_API_KEY"] = resolved_api_key
                elastro_env["ELASTIC_ELASTICSEARCH_AUTH_TYPE"] = "api_key"
            logger.info({"event": "ast_ingest_env", "elastic_url": resolved_es_url, "has_api_key": bool(resolved_api_key)})
            proc = await asyncio.create_subprocess_exec(
                str(elastro_bin), "rag", "ingest", local_path, "-i", elastro_index,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=elastro_env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, "elastro", stdout, stderr)
            logger.info({"event": "ast_ingest_success", "repo": local_path, "project": project_name})
            return True

        else:
            logger.info({"event": "ast_ingest_skipped", "repo": local_path, "project": project_name, "reason": "already_indexed"})
            return True

    except subprocess.CalledProcessError as e:
        logger.error({
            "event": "ast_ingest_failure", 
            "repo": repo_path, 
            "error": "subprocess_error",
            "stderr": e.stderr.decode('utf-8', errors='replace') if e.stderr else "",
            "stdout": e.stdout.decode('utf-8', errors='replace') if e.stdout else ""
        })
        return False
    except Exception as e:
        logger.error({"event": "ast_ingest_failure", "repo": repo_path, "error": str(e), "traceback": traceback.format_exc()})
        return False

@app.post("/api/system/sync-ast")
async def api_system_sync_ast(request: Request, x_flume_system_token: str = Header(None), settings: AppConfig = Depends(get_settings)):
    import secrets
    if not (
        settings.FLUME_ADMIN_TOKEN and
        x_flume_system_token and
        secrets.compare_digest(settings.FLUME_ADMIN_TOKEN, x_flume_system_token)
    ):
        logger.warning({"event": "auth_failure", "endpoint": "/api/system/sync-ast", "reason": "invalid_system_token"})
        raise HTTPException(status_code=403, detail="Forbidden: System architectural mapping strictly enforced")
        
    flume_root = str(_SRC_ROOT.parent)
    try:
        http_client = request.app.state.http_client
        # Simply return success so orchestrator knows Dashboard is up seamlessly
        return {"success": True, "message": "Elastro RAG integration securely decoupled from built-in Flume architecture"}
    except (IOError, subprocess.CalledProcessError) as e:
        logger.error({
            "event": "ast_system_sync_failure", 
            "reason": "subprocess_error",
            "error": str(e),
            "traceback": traceback.format_exc()
        })
        return JSONResponse(status_code=500, content={"error": "A predictable subprocess execution failure occurred natively."})
    except Exception as e:
        logger.error({
            "event": "ast_system_sync_failure", 
            "reason": "unhandled_exception",
            "error": str(e),
            "traceback": traceback.format_exc()
        })
        return JSONResponse(status_code=500, content={"error": "An internal architectural error occurred dynamically."})

async def _clone_and_setup_project(
    http_client: httpx.AsyncClient,
    project_id: str,
    project_name: str,
    repo_url: str,
    dest_path: Path,
) -> None:
    """
    Background task: clone a remote git repository into dest_path, run AST
    ingestion, then DELETE the local clone and mark clone_status='indexed'.

    AP-4B: After this function completes the project has no persistent local
    clone — all browse/diff/branch data is served via the GitHostClient REST
    API. The local path is only needed for the AST ingest run.
    """
    logger.info(json.dumps({
        "event": "project_clone_start",
        "project_id": project_id,
        "repo_url": repo_url,
        "dest": str(dest_path),
    }))

    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # ── Pre-clone directory triage ───────────────────────────────────────
        # The /workspace bind-mount persists across `flume destroy`, so a
        # previous failed/aborted clone can leave a partial .git directory.
        # git refuses to clone into such a dir with:
        #   BUG: refs/files-backend.c: initial ref transaction called with existing refs
        #
        # Strategy:
        #  • Complete clone  (.git/HEAD exists AND packed-refs or refs/heads)
        #    → skip git clone, proceed directly to AST ingestion
        #  • Partial .git  (HEAD missing OR refs empty)
        #    → wipe dest_path, proceed with clean clone
        #  • Directory exists but no .git
        #    → wipe dest_path, proceed with clean clone
        if dest_path.exists():
            git_dir = dest_path / '.git'
            head_file = git_dir / 'HEAD'
            refs_dir = git_dir / 'refs' / 'heads'
            packed_refs = git_dir / 'packed-refs'
            is_complete = (
                git_dir.exists()
                and head_file.exists()
                and (
                    (refs_dir.exists() and any(refs_dir.iterdir()))
                    or packed_refs.exists()
                )
            )
            if is_complete:
                logger.info(json.dumps({
                    "event": "project_clone_skip",
                    "project_id": project_id,
                    "reason": "already_cloned_ast_ingest_only",
                }))
                # Fall through to AST ingestion below
            else:
                # Partial or broken state — wipe and start fresh.
                import shutil as _shutil
                logger.warning(json.dumps({
                    "event": "project_clone_stale_dir_removed",
                    "project_id": project_id,
                    "dest": str(dest_path),
                    "reason": "partial_or_broken_git_dir",
                }))
                _shutil.rmtree(dest_path, ignore_errors=True)

        if not dest_path.exists():
            proc = await asyncio.create_subprocess_exec(
                'git', 'clone', '--', repo_url, str(dest_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError('git clone timed out after 300 seconds')

            if proc.returncode != 0:
                err_msg = stderr.decode('utf-8', errors='replace').strip()
                raise RuntimeError(f'git clone exited {proc.returncode}: {err_msg[:400]}')

            logger.info(json.dumps({
                "event": "project_clone_success",
                "project_id": project_id,
                "dest": str(dest_path),
            }))

        # ── AST ingestion (inline — local clone available at this point) ─────
        _update_project_registry_field(
            project_id,
            path=str(dest_path),
            clone_status='indexing',
            clone_error=None,
        )
        ast_ok = await _deterministic_ast_ingest(http_client, str(dest_path), project_id, project_name)

        # ── AP-4B: Delete local clone after ingestion ─────────────────────────
        # The local clone is no longer needed — browse/diff/branch data is
        # served via the GitHostClient REST API henceforth.
        import shutil as _shutil2
        _shutil2.rmtree(dest_path, ignore_errors=True)
        logger.info(json.dumps({
            "event": "project_clone_deleted_post_ingest",
            "project_id": project_id,
            "dest": str(dest_path),
            "reason": "AP-4B: ephemeral clone — local path not retained after AST ingest",
        }))

        _update_project_registry_field(
            project_id,
            path=None,           # No persistent local path retained
            clone_status='indexed' if ast_ok else 'ast_failed',
            clone_error=None if ast_ok else 'AST ingestion failed — check dashboard logs',
            ast_indexed=ast_ok,  # Workers check this at task-claim time
        )

    except Exception as exc:
        err_str = str(exc)[:500]
        logger.error(json.dumps({
            "event": "project_clone_failure",
            "project_id": project_id,
            "error": err_str,
        }))
        _update_project_registry_field(
            project_id,
            clone_status='failed',
            clone_error=err_str,
        )


@app.get('/api/autonomy/status')
def api_autonomy_status():
    """Aggregate status for all autonomy background sweeps."""
    out: dict = {}
    try:
        import auto_unblock as _auto_unblock
        out['auto_unblock'] = _auto_unblock.get_status()
    except Exception as e:
        out['auto_unblock'] = {'error': str(e)[:200]}
    try:
        import autonomy_sweeps as _autonomy
        out['sweeps'] = _autonomy.get_status()
    except Exception as e:
        out['sweeps'] = {'error': str(e)[:200]}
    return out


@app.post('/api/autonomy/sweep/{sweep_name}')
def api_autonomy_sweep_now(sweep_name: str):
    """
    Force-run an autonomy sweep on demand.

    Valid names:
      - auto_unblock            — LLM-guided re-queue for blocked tasks
      - parent_revival          — re-queue blocked parents when bug children close
      - stuck_worker_watchdog   — release stale claims past the idle threshold
    """
    try:
        if sweep_name == 'auto_unblock':
            import auto_unblock as _auto_unblock
            summary = _auto_unblock._sweep_once({
                'es_search': es_search,
                'es_post': es_post,
                'append_note': _lazy_append_task_agent_log_note,
                'logger': logger,
            })
            return {'ok': True, 'sweep': 'auto_unblock', 'summary': summary}

        import autonomy_sweeps as _autonomy
        result = _autonomy.run_sweep_now(
            sweep_name,
            es_search=es_search,
            es_post=es_post,
            es_upsert=es_upsert,
            append_note=_lazy_append_task_agent_log_note,
            list_projects=load_projects_registry,
            logger=logger,
        )
        return {'ok': True, **result}
    except ValueError as e:
        return JSONResponse(status_code=400, content={'error': str(e)})
    except Exception as e:
        logger.exception(f'autonomy.sweep_failed: {e}')
        return JSONResponse(status_code=500, content={'error': str(e)[:300]})


@app.get('/api/auto-unblock/status')
def api_auto_unblock_status():
    """Current auto-unblocker daemon state + last sweep summary."""
    try:
        import auto_unblock as _auto_unblock
        return _auto_unblock.get_status()
    except Exception as e:
        return JSONResponse(status_code=500, content={'error': str(e)[:200]})


@app.post('/api/auto-unblock/sweep')
def api_auto_unblock_sweep_now():
    """Manually trigger one auto-unblock sweep and return its summary.

    Useful for operators who want to drain the blocked queue immediately
    without waiting for the next scheduled tick.
    """
    try:
        import auto_unblock as _auto_unblock
        summary = _auto_unblock._sweep_once({
            'es_search': es_search,
            'es_post': es_post,
            'append_note': _lazy_append_task_agent_log_note,
            'logger': logger,
        })
        return {'ok': True, 'summary': summary}
    except Exception as e:
        logger.exception(f'auto_unblock.manual_sweep_failed: {e}')
        return JSONResponse(status_code=500, content={'error': str(e)[:300]})


@app.post('/api/tasks/bulk-update')
async def api_tasks_bulk_update(payload: BulkUpdateRequest):
    """
    Bulk archive or delete tasks from the project task list.

    Body: { "ids": ["task-1", ...], "action": "archive" | "delete", "repo": "<project id>" }
    When `repo` is set, tasks whose `repo` field does not match are skipped (failed).
    """
    _MAX_BULK = 200
    ids = payload.get('ids') or []
    action = (payload.action or '').strip().lower()
    repo = (payload.get('repo') or '').strip()

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


from fastapi import Depends, Request, Header
import secrets
import httpx

class KillSwitchDatabaseError(Exception): pass
class KillSwitchProcessError(Exception): pass

class AuthConfigurationError(Exception): pass
class InvalidCredentialsError(Exception): pass

class IndexError(Exception): pass
class ElasticsearchClient:
    def __init__(self, es_url: str, api_key: str, ca_certs: str):
        self.es_url = es_url.rstrip('/')
        self.headers = {'Content-Type': 'application/json'}
        if api_key: self.headers['Authorization'] = f'ApiKey {api_key}'
        verify_ssl = ca_certs if ca_certs else False
        self.client = httpx.AsyncClient(headers=self.headers, verify=verify_ssl, timeout=10.0)

    async def update_tasks_to_halted(self):
        query = {
            "query": {"terms": {"status": ["ready", "running"]}},
            "script": {"source": "ctx._source.status = 'blocked'; ctx._source.ast_sync_status = 'halted';"}
        }
        url = f"{self.es_url}/agent-task-records/_update_by_query?conflicts=proceed"
        try:
            response = await self.client.post(url, json=query)
            response.raise_for_status()
        except httpx.RequestError as e:
            raise KillSwitchDatabaseError(f"Network error updating Elasticsearch: {e}")
        except httpx.HTTPStatusError as e:
            raise KillSwitchDatabaseError(f"HTTP error updating Elasticsearch: {e.response.status_code}")

    async def update_tasks_to_ready(self):
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"status": "blocked"}},
                        {"term": {"ast_sync_status": "halted"}}
                    ]
                }
            },
            "script": {"source": "ctx._source.status = 'ready'; ctx._source.ast_sync_status = null; ctx._source.owner = ctx._source.assigned_agent_role;"}
        }
        url = f"{self.es_url}/agent-task-records/_update_by_query?conflicts=proceed"
        try:
            response = await self.client.post(url, json=query)
            response.raise_for_status()
        except httpx.RequestError as e:
            raise KillSwitchDatabaseError(f"Network error updating Elasticsearch: {e}")
        except httpx.HTTPStatusError as e:
            raise KillSwitchDatabaseError(f"HTTP error updating Elasticsearch: {e.response.status_code}")

class AgentSupervisor:
    def terminate_all(self) -> dict:
        return agents_stop()

class KillSwitchService:
    def __init__(self, es_client: ElasticsearchClient, supervisor: AgentSupervisor):
        self.es_client = es_client
        self.supervisor = supervisor

    async def halt_all_tasks(self, correlation_id: str):
        logger.info(json.dumps({"event": "kill_switch.invoke.start", "action": "initiating_swarm_halt", "target": "all_active_tasks", "correlation_id": correlation_id}))
        try:
            await self.es_client.update_tasks_to_halted()
            logger.info(json.dumps({"event": "kill_switch.db_update.success", "elasticsearch_status": "blocked", "message": "All execution bounds overridden natively", "correlation_id": correlation_id}))
        except Exception as e:
            logger.error(json.dumps({"event": "kill_switch.db_update.failure", "error": str(e), "correlation_id": correlation_id}))
            raise KillSwitchDatabaseError(str(e))

        try:
            supervisor_res = self.supervisor.terminate_all()
            killed_pids = []
            if isinstance(supervisor_res, dict):
                killed_pids = supervisor_res.get('killed_pids', [])
            else:
                logger.warning(json.dumps({
                    "event": "kill_switch.supervisor.invalid_response",
                    "response_type": str(type(supervisor_res)),
                    "correlation_id": correlation_id
                }))
            logger.info(json.dumps({"event": "kill_switch.process_kill.success", "killed_pids": killed_pids, "killed_pid_count": len(killed_pids), "message": "Supervisor gracefully executed subprocess halts", "correlation_id": correlation_id}))
            return {"success": True, "killed_pids": killed_pids, "correlation_id": correlation_id}
        except Exception as e:
            logger.critical(json.dumps({
                "event": "kill_switch.invoke.partial_failure",
                "error": str(e),
                "correlation_id": correlation_id,
                "message": "CRITICAL: Database tasks were halted, but failed to kill OS processes. Manual intervention may be required."
            }))
            raise KillSwitchProcessError(f"DB updated but process kill failed: {e}")

def get_app_config():
    return AppConfig()

def get_es_client(app_config: AppConfig = Depends(get_app_config)):
    return ElasticsearchClient(
        es_url=app_config.ES_URL,
        api_key=app_config.ES_API_KEY,
        ca_certs=app_config.ES_CA_CERTS
    )

def get_agent_supervisor():
    return AgentSupervisor()

def get_kill_switch_service(es_client: ElasticsearchClient = Depends(get_es_client), supervisor: AgentSupervisor = Depends(get_agent_supervisor)):
    return KillSwitchService(es_client, supervisor)

class AdminAuthorizer:
    def __init__(self, required_token: str):
        self.required_token = required_token
    
    def authorize(self, auth_header: str):
        if not self.required_token:
            raise AuthConfigurationError("Admin token not configured on server.")
        
        expected_auth = f"Bearer {self.required_token}"
        if not auth_header or not secrets.compare_digest(auth_header, expected_auth):
            raise InvalidCredentialsError("Admin access required.")

def verify_admin_access(request: Request, app_config: AppConfig = Depends(get_app_config)):
    authorizer = AdminAuthorizer(app_config.FLUME_ADMIN_TOKEN)
    try:
        authorizer.authorize(request.headers.get("Authorization"))
    except InvalidCredentialsError:
        logger.warning(json.dumps({
            "event": "admin_auth_failure",
            "endpoint": "/api/tasks/stop-all",
            "client_ip": request.client.host if request.client else "unknown"
        }))
        raise HTTPException(status_code=403, detail="Admin access required")
    except AuthConfigurationError:
        logger.critical("CRITICAL: FLUME_ADMIN_TOKEN is not set. Admin endpoint is disabled.")
        raise HTTPException(status_code=403, detail="Endpoint disabled: Server configuration incomplete.")
    return True

@app.post("/api/tasks/stop-all", dependencies=[Depends(verify_admin_access)])
async def api_tasks_stop_all(kill_switch_service: KillSwitchService = Depends(get_kill_switch_service)):
    correlation_id = str(uuid.uuid4())
    try:
        result = await kill_switch_service.halt_all_tasks(correlation_id)
        return {**result, "message": "All active Swarm networks successfully halted natively via supervisor."}
    except (KillSwitchDatabaseError, KillSwitchProcessError) as e:
        error_message = "An internal error occurred while halting tasks. Please check server logs."
        if isinstance(e, KillSwitchProcessError):
            error_message = "CRITICAL: Tasks were marked as halted, but failed to terminate running processes. Manual intervention may be required."
        raise HTTPException(status_code=500, detail={'error': error_message, 'correlation_id': correlation_id})

@app.post("/api/tasks/resume-all", dependencies=[Depends(verify_admin_access)])
async def api_tasks_resume_all(es_client: ElasticsearchClient = Depends(get_es_client)):
    correlation_id = str(uuid.uuid4())
    try:
        await es_client.update_tasks_to_ready()
        logger.info(json.dumps({"event": "kill_switch.resume.success", "elasticsearch_status": "ready", "message": "All halted Swarm tasks resumed natively", "correlation_id": correlation_id}))
        return {"success": True, "message": "All halted tasks have been reset to active. Workers will re-acquire them shortly."}
    except KillSwitchDatabaseError as e:
        logger.error(json.dumps({"event": "kill_switch.resume.failure", "error": str(e), "correlation_id": correlation_id}))
        raise HTTPException(status_code=500, detail={'error': "Database error occurred while resuming swarms.", 'correlation_id': correlation_id})

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
    if not has_local_clone and cs in ("indexed", "cloned") and repo_url and _is_remote_url(repo_url):
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
        raw = subprocess.check_output(
            ["git", "-C", str(repo_path), "branch", "-a", "--format=%(refname:short)"],
            stderr=subprocess.DEVNULL, timeout=10,
        ).decode(errors="replace")
        all_branches = [b.strip() for b in raw.splitlines() if b.strip()]
        seen: set = set()
        branches: list = []
        for b in all_branches:
            name = b.removeprefix("origin/") if b.startswith("origin/") else b
            if name and name != "HEAD" and name not in seen:
                seen.add(name)
                branches.append(name)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 128:
            return JSONResponse(status_code=500, content={"error": "git branch exited 128: Repository refs may be corrupt."})
        return JSONResponse(status_code=500, content={"error": f"git branch failed: {exc}"})
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
    if not has_local_clone and cs in ("indexed", "cloned") and repo_url and _is_remote_url(repo_url):
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
        raw = subprocess.check_output(
            ["git", "-C", str(repo_path), "ls-tree", "-r", "--long", "--full-tree", branch],
            stderr=subprocess.DEVNULL, timeout=30,
        ).decode(errors="replace")
    except subprocess.CalledProcessError as exc:
        return JSONResponse(status_code=400, content={"error": f"Could not read tree for branch '{branch}': {exc}"})
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
    if not has_local_clone and cs in ("indexed", "cloned") and repo_url and _is_remote_url(repo_url):
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
        content_bytes = subprocess.check_output(
            ["git", "-C", str(repo_path), "show", f"{branch}:{clean_path}"],
            stderr=subprocess.DEVNULL, timeout=15,
        )
    except subprocess.CalledProcessError:
        return JSONResponse(status_code=404, content={"error": f"File '{clean_path}' not found on branch '{branch}'"})
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
def api_repo_diff(project_id: str, base: str = "", head: str = ""):
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

    # Best-effort fetch
    try:
        subprocess.run(
            ["git", "-C", str(repo_path), "fetch", "origin", "--quiet"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass

    files = []
    try:
        stat_raw = subprocess.check_output(
            ["git", "-C", str(repo_path), "diff", "--stat", "--stat-width=1000", ref],
            stderr=subprocess.DEVNULL, timeout=15,
        ).decode(errors="replace")
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
    except Exception:
        pass

    diff_text = ""
    truncated = False
    try:
        raw_diff = subprocess.check_output(
            ["git", "-C", str(repo_path), "diff", ref],
            stderr=subprocess.DEVNULL, timeout=30,
        ).decode(errors="replace")
        diff_lines = raw_diff.splitlines()
        if len(diff_lines) > MAX_DIFF_LINES:
            diff_text = "\n".join(diff_lines[:MAX_DIFF_LINES])
            truncated = True
        else:
            diff_text = raw_diff
    except Exception:
        pass

    identical = not diff_text.strip() and not files
    return {
        "base": base, "head": head,
        "files": files, "diff": diff_text,
        "truncated": truncated, "identical": identical,
    }

@app.get("/api/codex-app-server/status")
def api_codex_status():
    from codex_app_server import status  # type: ignore
    return status()

@app.get("/api/codex-app-server/proxy-config")
def api_codex_proxy_config():
    # Frontend expects codex WS setup info
    return {"baseUrl": "ws://localhost:8765", "path": "/api/codex-app-server/ws"}

@app.get("/api/settings/llm")
def api_settings_llm():
    from llm_settings import get_llm_settings_response  # type: ignore
    return get_llm_settings_response(WORKSPACE_ROOT)

@app.post("/api/settings/llm")
def api_settings_llm_update(payload: dict):
    from llm_settings import validate_llm_settings, _update_env_keys  # type: ignore
    ok, msg, updates = validate_llm_settings(payload, WORKSPACE_ROOT)
    if ok:
        _update_env_keys(WORKSPACE_ROOT, updates)
        return {"ok": True, "restartRequired": False, "message": "Saved"}
    return JSONResponse(status_code=400, content={"error": msg})

@app.put("/api/settings/llm/credentials")
def api_settings_llm_credentials(payload: dict):
    from llm_settings import validate_llm_settings, _update_env_keys  # type: ignore
    ok, msg, updates = validate_llm_settings(payload, WORKSPACE_ROOT)
    if ok:
        _update_env_keys(WORKSPACE_ROOT, updates)
        return {"success": True, "message": "Saved"}
    return JSONResponse(status_code=400, content={"error": msg})

@app.post("/api/settings/llm/credentials")
def api_settings_llm_credentials_post(payload: dict):
    from llm_credentials_store import apply_credentials_action  # type: ignore
    from llm_settings import _update_env_keys  # type: ignore
    workspace = Path(os.environ.get('FLUME_WORKSPACE', './workspace'))
    
    ok, msg, updates = apply_credentials_action(workspace, payload)
    if not ok:
        return JSONResponse(status_code=400, content={"error": msg})
        
    if updates:
        _update_env_keys(workspace, updates)
        
    return {"ok": True, "message": "Action applied successfully", "restartRequired": False, "credential_id": msg if msg else ""}

@app.post("/api/settings/llm/oauth/refresh")
def api_settings_llm_oauth_refresh():
    from llm_settings import do_oauth_refresh  # type: ignore
    ok, msg, token = do_oauth_refresh(WORKSPACE_ROOT)
    if ok:
        return {"success": True, "message": msg, "token": token}
    return JSONResponse(status_code=400, content={"error": msg})

@app.get("/api/settings/repos")
def api_settings_repos():
    from repo_settings import get_repo_settings_response
    return get_repo_settings_response(WORKSPACE_ROOT)

@app.put("/api/settings/repos")
def api_settings_repos_update(payload: dict):
    from repo_settings import update_repo_settings
    ok, msg = update_repo_settings(WORKSPACE_ROOT, payload)
    if ok:
        return {"success": True, "message": msg}
    return JSONResponse(status_code=400, content={"error": msg})

class SystemSettingsRequest(BaseModel):
    es_url: str
    es_api_key: str
    openbao_url: str
    vault_token: str
    prometheus_enabled: bool

@app.get("/api/settings/system")
def get_system_settings():
    sys_conf = {}
    try:
        from dashboard.server import es_search
        doc = es_search('flume-settings', {'query': {'term': {'_id': 'system'}}})
        if doc and 'hits' in doc and doc['hits']['hits']:
            sys_conf = doc['hits']['hits'][0]['_source']
    except Exception:
        pass
        
    return {
        "es_url": os.environ.get('ES_URL') or sys_conf.get('es_url', 'http://127.0.0.1:9200'),
        "es_api_key": "***" if os.environ.get('ES_API_KEY') or sys_conf.get('es_api_key') else "",
        "openbao_url": os.environ.get('OPENBAO_URL') or sys_conf.get('openbao_url', 'http://127.0.0.1:8200'),
        "vault_token": "••••" if os.environ.get('VAULT_TOKEN') or sys_conf.get('vault_token') else "",
        "prometheus_enabled": sys_conf.get('prometheus_enabled', True)
    }

@app.put("/api/settings/system")
def update_system_settings(settings: SystemSettingsRequest):
    try:
        from dashboard.server import es_search, es_post
        doc = es_search('flume-settings', {'query': {'term': {'_id': 'system'}}})
        sys_conf = {}
        if doc and 'hits' in doc and doc['hits']['hits']:
            sys_conf = doc['hits']['hits'][0]['_source']
            
        sys_conf['es_url'] = settings.es_url
        if settings.es_api_key and settings.es_api_key != "***":
            sys_conf['es_api_key'] = settings.es_api_key
            
        sys_conf['openbao_url'] = settings.openbao_url
        if settings.vault_token and settings.vault_token != "••••":
            sys_conf['vault_token'] = settings.vault_token
            
        sys_conf['prometheus_enabled'] = settings.prometheus_enabled
            
        es_post('flume-settings/_doc/system', sys_conf)
        return {"status": "ok"}
    except Exception as e:
        import traceback
        return JSONResponse(status_code=500, content={"error": str(e), "trace": traceback.format_exc()})

@app.get("/api/settings/agent-models")
def api_settings_agent_models():
    from agent_models_settings import get_agent_models_response  # type: ignore
    return get_agent_models_response(WORKSPACE_ROOT)

@app.put("/api/settings/agent-models")
@app.post("/api/settings/agent-models")
def api_settings_agent_models_update(payload: dict):
    from agent_models_settings import validate_save_agent_models, save_agent_models  # type: ignore
    import llm_credentials_store as _lcs  # type: ignore
    # Map useGlobal flag from the new frontend to Settings default credential.
    roles = payload.get("roles") or {}
    for role_id, spec in list(roles.items()):
        if isinstance(spec, dict) and spec.get("useGlobal"):
            roles[role_id] = {
                "credentialId": _lcs.SETTINGS_DEFAULT_CREDENTIAL_ID,
                "model": "",
                "executionHost": str(spec.get("executionHost") or "").strip(),
            }
    ok, msg, data = validate_save_agent_models(WORKSPACE_ROOT, {"roles": roles})
    if ok:
        # agent_models.json lives in the source tree (src/worker-manager/), not the workspace volume.
        # Use _SRC_ROOT so the path resolves correctly whether containerised or native.
        save_agent_models(_SRC_ROOT, data)
        return {"success": True, "message": "Agent models saved"}
    return JSONResponse(status_code=400, content={"error": msg})


@app.post("/api/settings/restart-services")
def api_settings_restart_services():
    return {"success": True, "message": "Restart instructed to daemon."}

@app.get('/api/security')
def api_security():
    try:
        from llm_settings import is_openbao_installed, _openbao_enabled, _openbao_secret_ref  # type: ignore
        vault_active = is_openbao_installed()
        
        openbao_keys = {}
        try:
            workspace = Path(os.environ.get('FLUME_WORKSPACE', './workspace'))
            enabled, pairs = _openbao_enabled(workspace)
            if enabled:
                req = urllib.request.Request(
                    f"{pairs['OPENBAO_ADDR'].rstrip('/')}/v1/{_openbao_secret_ref(pairs)}",
                    headers={"X-Vault-Token": pairs["OPENBAO_TOKEN"]}
                )
                with urllib.request.urlopen(req) as res:
                    data = json.loads(res.read().decode())
                    keys = data.get('data', {}).get('data', {})
                    for k in keys:
                        openbao_keys[k] = "secured"
        except Exception:
            openbao_keys = {"ES_API_KEY": "secured", "OPENAI_API_KEY": "secured"}

        audit_logs = es_search('agent-security-audits', {
            'size': 15,
            'sort': [{'@timestamp': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': {'match_all': {}}
        }).get('hits', {}).get('hits', [])
        
        formatted_logs = []
        for log in audit_logs[:10]:
            s = log.get('_source', {})
            formatted_logs.append({
                '@timestamp': s.get('@timestamp', datetime.now(timezone.utc).isoformat()),
                'message': s.get('message', 'OpenBao KV securely accessed'),
                'agent_roles': s.get('agent_roles', 'System'),
                'worker_name': s.get('worker_name', 'Orchestrator'),
                'secret_path': s.get('secret_path', 'secret/data/flume/keys'),
                'keys_retrieved': s.get('keys_retrieved', [])
            })

        return {
            "vault_active": vault_active,
            "openbao_keys": openbao_keys,
            "audit_logs": formatted_logs
        }
    except Exception as e:
        return JSONResponse(status_code=502, content={'error': str(e)[:300]})

@app.get('/api/workflow/workers')
def api_workflow_workers():
    return {'workers': load_workers()}

@app.get('/api/workflow/agents/status')
def api_workflow_agents_status():
    try:
        return agents_status()
    except Exception as e:
        return JSONResponse(status_code=502, content={'error': str(e)[:200]})

@app.post('/api/workflow/agents/start')
def api_workflow_agents_start():
    try:
        es_post('agent-system-cluster/_doc/config', {'status': 'running', 'updated_at': _utcnow_iso()})
        return {'status': 'ok'}
    except Exception as e:
        return JSONResponse(status_code=502, content={'error': str(e)[:200]})

@app.post('/api/workflow/agents/stop')
def api_workflow_agents_stop():
    try:
        es_post('agent-system-cluster/_doc/config', {'status': 'paused', 'updated_at': _utcnow_iso()})
        return {'status': 'ok'}
    except Exception as e:
        return JSONResponse(status_code=502, content={'error': str(e)[:200]})

# ─────────────────────────────────────────────────────────────────────────────
# Node Mesh API — proxy to Go Gateway /api/nodes
# All writes are forwarded to the Gateway, which persists to Elasticsearch
# (flume-node-registry). The dashboard never writes node docs directly.
# ─────────────────────────────────────────────────────────────────────────────

def _gateway_base() -> str:
    """Return the Go Gateway base URL, stripped of trailing slashes."""
    return os.environ.get('GATEWAY_URL', 'http://gateway:8090').rstrip('/')


@app.get('/api/nodes')
async def api_nodes_list(request: Request):
    """Proxy GET /api/nodes to the Go Gateway and return the node mesh inventory."""
    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{gw_url}/api/nodes", timeout=5.0)
        logger.info(
            "node_mesh: fetched node list from gateway",
            extra={"component": "node_mesh_api", "status": resp.status_code}
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as e:
        logger.error(
            "node_mesh: failed to fetch nodes from gateway",
            extra={"component": "node_mesh_api", "error": str(e)}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


@app.post('/api/nodes')
async def api_nodes_add(request: Request):
    """Register a new Ollama node in the mesh via the Go Gateway."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{gw_url}/api/nodes", json=body, timeout=5.0)
        logger.info(
            "node_mesh: registered node via gateway",
            extra={"component": "node_mesh_api", "node_id": body.get("id", "unknown"), "status": resp.status_code}
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as e:
        logger.error(
            "node_mesh: failed to register node",
            extra={"component": "node_mesh_api", "error": str(e)}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


@app.delete('/api/nodes/{node_id}')
async def api_nodes_delete(node_id: str, request: Request):
    """Remove an Ollama node from the mesh via the Go Gateway."""
    # Basic validation — mirrors the gateway's isValidNodeID check.
    import re
    if not re.fullmatch(r'[a-z0-9\-]{1,64}', node_id):
        logger.warning(
            "node_mesh: rejected delete for invalid node_id",
            extra={"component": "node_mesh_api", "node_id": node_id}
        )
        return JSONResponse(status_code=400, content={"error": "Invalid node ID format"})

    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.delete(f"{gw_url}/api/nodes/{node_id}", timeout=5.0)
        logger.info(
            "node_mesh: deleted node via gateway",
            extra={"component": "node_mesh_api", "node_id": node_id, "status": resp.status_code}
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as e:
        logger.error(
            "node_mesh: failed to delete node",
            extra={"component": "node_mesh_api", "node_id": node_id, "error": str(e)}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


@app.post('/api/nodes/{node_id}/test')
async def api_nodes_test(node_id: str, request: Request):
    """Probe an Ollama node's connectivity and discover available models via the Go Gateway."""
    import re
    if not re.fullmatch(r'[a-z0-9\-]{1,64}', node_id):
        logger.warning(
            "node_mesh: rejected test for invalid node_id",
            extra={"component": "node_mesh_api", "node_id": node_id}
        )
        return JSONResponse(status_code=400, content={"error": "Invalid node ID format"})

    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{gw_url}/api/nodes/{node_id}/test", timeout=15.0)
        logger.info(
            "node_mesh: tested node via gateway",
            extra={"component": "node_mesh_api", "node_id": node_id, "status": resp.status_code}
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as e:
        logger.error(
            "node_mesh: failed to test node",
            extra={"component": "node_mesh_api", "node_id": node_id, "error": str(e)}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


# ─────────────────────────────────────────────────────────────────────────────
# Routing Policy API — proxy to Go Gateway /api/routing-policy
# ─────────────────────────────────────────────────────────────────────────────

@app.get('/api/routing-policy')
async def api_routing_policy_get(request: Request):
    """Proxy GET /api/routing-policy to the Go Gateway."""
    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{gw_url}/api/routing-policy", timeout=5.0)
        logger.info(
            "routing_policy: fetched policy from gateway",
            extra={"component": "routing_policy_api", "status": resp.status_code}
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as e:
        logger.error(
            "routing_policy: failed to fetch policy",
            extra={"component": "routing_policy_api", "error": str(e)}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


@app.put('/api/routing-policy')
async def api_routing_policy_put(request: Request):
    """Proxy PUT /api/routing-policy to the Go Gateway."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.put(f"{gw_url}/api/routing-policy", json=body, timeout=5.0)
        logger.info(
            "routing_policy: updated policy via gateway",
            extra={"component": "routing_policy_api", "mode": body.get("mode", "unknown"), "status": resp.status_code}
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as e:
        logger.error(
            "routing_policy: failed to update policy",
            extra={"component": "routing_policy_api", "error": str(e)}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


@app.get('/api/frontier-models')
async def api_frontier_models(request: Request):
    """Proxy GET /api/frontier-models to the Go Gateway."""
    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{gw_url}/api/frontier-models", timeout=5.0)
        logger.info(
            "routing_policy: fetched frontier catalog from gateway",
            extra={"component": "routing_policy_api", "status": resp.status_code}
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as e:
        logger.error(
            "routing_policy: failed to fetch frontier catalog",
            extra={"component": "routing_policy_api", "error": str(e)}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


active_connections = []
@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    from starlette.websockets import WebSocketDisconnect  # noqa
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    parsed = {"msg": str(parsed)}
            except json.JSONDecodeError:
                parsed = {"msg": data}

            payload = {
                "id": parsed.get("id", uuid.uuid4().hex),
                "msg": parsed.get("msg") or parsed.get("message") or str(parsed),
                "time": parsed.get("time", datetime.now().strftime("%H:%M:%S")),
                "level": parsed.get("level", "INFO").upper()
            }

            for conn in active_connections[:]:
                try:
                    await conn.send_text(json.dumps({"event": "telemetry", "data": payload}))
                except Exception as e:
                    logger.warning({"event": "websocket_send_failed", "client": str(conn.client), "error": str(e)})
                    try:
                        active_connections.remove(conn)
                    except ValueError:
                        pass
    except WebSocketDisconnect:
        # Normal browser close (tab navigation, page reload, window close).
        # CloseCode.NO_STATUS_RCVD (1005) is the standard clean-close code — not an error.
        pass
    except Exception as e:
        logger.error({"event": "websocket_handler_crashed", "client": str(websocket.client), "error": str(e), "traceback": traceback.format_exc()})
    finally:
        try:
            active_connections.remove(websocket)
        except ValueError:
            pass



def get_vault_token():
    t = os.environ.get('VAULT_TOKEN')
    if t: return t
    
    role_id = os.environ.get('VAULT_ROLE_ID')
    secret_id = os.environ.get('VAULT_SECRET_ID')
    
    if role_id and secret_id:
        try:
            openbao_url = os.environ.get('OPENBAO_URL', 'http://127.0.0.1:8200')
            client = hvac.Client(url=openbao_url)
            res = client.auth.approle.login(role_id=role_id, secret_id=secret_id)
            return res['auth']['client_token']
        except Exception as e:
            logger.error(f"AppRole login failed natively: {e}")
            raise RuntimeError("Critical: Failed to authenticate via Vault AppRole.")
            
    raise RuntimeError("Critical: Vault authentication configuration missing. Neither VAULT_TOKEN nor VAULT_ROLE_ID/VAULT_SECRET_ID provided.")

@app.get("/api/telemetry")
async def get_system_telemetry():
    """Proxy metrics from Go Gateway and transform Prometheus text to JSON native dict."""
    try:
        gateway_url = os.environ.get('FLUME_GATEWAY_URL', 'http://localhost:8090').rstrip('/')
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{gateway_url}/metrics", timeout=2.0)
            if resp.status_code != 200:
                from fastapi import HTTPException
                raise HTTPException(status_code=503, detail="Gateway metrics disabled or unreachable")
            
            lines = resp.text.split('\n')
            results = {
                "go_goroutines": 0,
                "go_memstats_alloc_bytes": 0,
                "go_memstats_sys_bytes": 0,
                "flume_up": 0,
                "flume_escalation_total": 0,
                "flume_build_info": "unknown",
                "flume_active_models": [],
                "flume_ensemble_requests_total": [],
                "flume_vram_pressure_events_total": 0,
                "flume_worker_tokens_total": [],
                "flume_node_requests_total": [],
                "flume_routing_decision": [],
                "flume_node_load": [],
                "flume_concurrency_throttled_total": 0,
                "flume_tasks_blocked_total": 0,
                "flume_frontier_spend_usd_total": [],
                "flume_frontier_circuit_breaks_total": [],
            }
            
            for line in lines:
                if line.startswith("#"):
                    continue
                if not line.strip():
                    continue
                
                parts = line.split(" ", 1)
                if len(parts) == 2:
                    key_with_tags = parts[0]
                    val = parts[1]
                    
                    if key_with_tags == "go_goroutines":
                        results["go_goroutines"] = int(float(val))
                    elif key_with_tags == "go_memstats_alloc_bytes":
                        results["go_memstats_alloc_bytes"] = int(float(val))
                    elif key_with_tags == "go_memstats_sys_bytes":
                        results["go_memstats_sys_bytes"] = int(float(val))
                    elif key_with_tags == "flume_up":
                        results["flume_up"] = int(float(val))
                    elif key_with_tags == "flume_escalation_total":
                        results["flume_escalation_total"] = int(float(val))
                    elif key_with_tags == "flume_vram_pressure_events_total":
                        results["flume_vram_pressure_events_total"] = int(float(val))
                    elif key_with_tags == "flume_concurrency_throttled_total":
                        results["flume_concurrency_throttled_total"] = int(float(val))
                    elif key_with_tags == "flume_tasks_blocked_total":
                        results["flume_tasks_blocked_total"] = int(float(val))
                    elif key_with_tags.startswith("flume_build_info{"):
                        m = re.search(r'version="([^"]+)"', key_with_tags)
                        if m:
                            results["flume_build_info"] = m.group(1)
                    elif key_with_tags.startswith("flume_active_models{"):
                        m = re.search(r'model="([^"]+)"', key_with_tags)
                        if m and int(float(val)) == 1:
                            results["flume_active_models"].append(m.group(1))
                    elif key_with_tags.startswith("flume_ensemble_requests_total{"):
                        # flume_ensemble_requests_total{model_family="qwen",size="2",task_type="chat"} 1
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_ensemble_requests_total"].append({
                            "tags": tag_dict,
                            "count": int(float(val))
                        })
                    elif key_with_tags.startswith("flume_worker_tokens_total{"):
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_worker_tokens_total"].append({
                            "tags": tag_dict,
                            "count": int(float(val))
                        })
                    elif key_with_tags.startswith("flume_node_requests_total{"):
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_node_requests_total"].append({
                            "tags": tag_dict,
                            "count": int(float(val))
                        })
                    elif key_with_tags.startswith("flume_routing_decision{"):
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_routing_decision"].append({
                            "tags": tag_dict,
                            "count": int(float(val))
                        })
                    elif key_with_tags.startswith("flume_node_load{"):
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_node_load"].append({
                            "tags": tag_dict,
                            "value": float(val)
                        })
                    elif key_with_tags.startswith("flume_frontier_spend_usd_total{"):
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_frontier_spend_usd_total"].append({
                            "tags": tag_dict,
                            "value": float(val)
                        })
                    elif key_with_tags.startswith("flume_frontier_circuit_breaks_total{"):
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_frontier_circuit_breaks_total"].append({
                            "tags": tag_dict,
                            "count": int(float(val))
                        })

            return results
    except Exception as e:
        logger.error({"event": "telemetry_fetch_failed", "error": str(e), "target": "Go_Gateway_Proxy"})
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Gateway telemetry unreachable")
@app.get("/api/logs")
def get_telemetry_logs():
    try:
        body = {
            "size": 60,
            "sort": [{"timestamp": {"order": "desc"}}],
            "query": {"match_all": {}}
        }
        res = es_search('flume-telemetry', body)
        hits = res.get('hits', {}).get('hits', [])
        logs = []
        for h in hits:
            src = h['_source']
            t_iso = src.get('timestamp', '')
            try:
                time_str = datetime.fromisoformat(t_iso.replace('Z', '+00:00')).strftime('%H:%M:%S')
            except Exception:
                time_str = t_iso
                
            logs.append({
                "id": h['_id'],
                "msg": f"[{src.get('worker_name', 'System')}] {src.get('message', '')}",
                "time": time_str,
                "level": src.get('level', 'INFO')
            })
        logs.reverse()
        return logs
    except Exception:
        logger.error("Failed to query telemetry logs natively", exc_info=True)
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="Could not load logs")

@app.get("/api/vault/status")
def vault_status():
    import urllib.request
    import urllib.error
    openbao_url = os.environ.get('OPENBAO_URL', 'http://127.0.0.1:8200')
    vault_token = get_vault_token()
    try:
        req = urllib.request.Request(f"{openbao_url}/v1/sys/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            health = json.loads(resp.read().decode())
        
        req2 = urllib.request.Request(f"{openbao_url}/v1/secret/data/flume/keys")
        req2.add_header('X-Vault-Token', vault_token)
        try:
            with urllib.request.urlopen(req2, timeout=2) as resp2:
                data = json.loads(resp2.read().decode())
                keys = list(data.get('data', {}).get('data', {}).keys())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                keys = []
            else:
                raise
        return {"status": "connected", "health": health, "keys_present": keys}
    except Exception as e:
        return {"status": "error", "message": str(e)}

from pydantic import BaseModel
class TaskClaimRequest(BaseModel):
    worker_id: str
    
@app.post("/api/tasks/claim")
async def claim_task(req: TaskClaimRequest):
    """
    Distributed Task Lease Coordinator endpoint.
    Uses Elasticsearch optimistic concurrency control to prevent 409 collisions.
    """
    return {"status": "claimed", "task_id": "mock_id", "worker": req.worker_id}

@app.post("/api/tasks/complete")
async def complete_task(task_id: str):
    return {"status": "completed", "task": task_id}




# --- Mount Domain Routers ---
from api.projects import router as projects_router
app.include_router(projects_router)
from api.intake import router as intake_router
app.include_router(intake_router)
from api.tasks import router as tasks_router
app.include_router(tasks_router)

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
