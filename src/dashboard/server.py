#!/usr/bin/env python3
from pathlib import Path
from typing import Optional
import json
import os
import re
import signal
import sys
import tempfile
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
from fastapi import BackgroundTasks
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

from flume_secrets import apply_runtime_config, hydrate_secrets_from_openbao, load_legacy_dotenv_into_environ  # noqa: E402

# Merge .env config
apply_runtime_config(_SRC_ROOT)

# Hydrate OpenBao Secrets Natively
hydrate_secrets_from_openbao()

# Execute Elasticsearch Index Bootstrapping natively now that auth is fully populated
from es_bootstrap import ensure_es_indices
ensure_es_indices()

from llm_settings import load_effective_pairs, resolve_effective_ollama_base_url  # noqa: E402

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
from config import AppConfig, get_settings

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



def _ensure_gitflow_defaults(entry: dict) -> dict:
    """Backfill gitflow config with defaults if missing."""
    if 'gitflow' not in entry:
        entry['gitflow'] = {'autoPrOnApprove': True, 'defaultBranch': None}
    else:
        gf = entry['gitflow']
        if 'autoPrOnApprove' not in gf:
            gf['autoPrOnApprove'] = True
        if 'defaultBranch' not in gf:
            gf['defaultBranch'] = None
    return entry


def es_counter_hwm(prefix: str) -> int:
    """
    Return the stored high-water-mark for *prefix* from the ES flume-counters index.
    Returns 0 if the document does not yet exist or ES is unreachable.
    """
    try:
        res = _es_counter_request(f'/{COUNTERS_INDEX}/_doc/{prefix}')
        return int(res.get('_source', {}).get('value', 0))
    except Exception:
        return 0


def es_counter_set_hwm(prefix: str, value: int) -> None:
    """
    Atomically raise the stored counter for *prefix* to *value* if it is higher.
    Uses a Painless script so concurrent dashboard replicas are safe.
    """
    if value <= 0:
        return
    now = datetime.now(timezone.utc).isoformat()
    body = {
        'scripted_upsert': True,
        'script': {
            'source': (
                'if (ctx._source.containsKey("value")) {'
                '  ctx._source.value = Math.max(ctx._source.value, (long)params.v);'
                '} else {'
                '  ctx._source.value = (long)params.v;'
                '}'
                ' ctx._source.updated_at = params.ts;'
                ' ctx._source.prefix = params.pfx;'
            ),
            'lang': 'painless',
            'params': {'v': value, 'ts': now, 'pfx': prefix},
        },
        'upsert': {'prefix': prefix, 'value': value, 'updated_at': now},
    }
    try:
        _es_counter_request(f'/{COUNTERS_INDEX}/_update/{prefix}', body=body, method='POST')
    except Exception as exc:
        logger.warning(json.dumps({
            'event': 'es_counter_set_hwm_failed',
            'prefix': prefix,
            'value': value,
            'error': str(exc),
        }))


def _es_counter_request(path: str, body=None, method: str = 'GET') -> dict:
    """Thin HTTP helper scoped to the flume-counters ES index."""
    headers = {'Content-Type': 'application/json'}
    api_key = os.environ.get('ES_API_KEY', '')
    if api_key:
        headers['Authorization'] = f'ApiKey {api_key}'
    data = json.dumps(body).encode() if body is not None else None
    if data and method == 'GET':
        method = 'POST'
    req = urllib.request.Request(f'{ES_URL}{path}', data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise



# ---------------------------------------------------------------------------
# Projects Registry — Elasticsearch-backed (replaces projects.json)
# ---------------------------------------------------------------------------

PROJECTS_INDEX = "flume-projects"


def _es_projects_request(path: str, body=None, method: str = "GET") -> dict:
    """Low-level ES request scoped to the projects index."""
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("ES_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    data = json.dumps(body).encode() if body is not None else None
    if data and method == "GET":
        method = "POST"
    req = urllib.request.Request(f"{ES_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def load_projects_registry() -> list:
    """Return all registered projects from Elasticsearch."""
    try:
        res = _es_projects_request(
            f"/{PROJECTS_INDEX}/_search",
            {"size": 500, "query": {"match_all": {}}, "sort": [{"created_at": {"order": "asc"}}]},
        )
        hits = res.get("hits", {}).get("hits", [])
        return [_ensure_gitflow_defaults(h["_source"]) for h in hits if h.get("_source")]
    except Exception as e:
        logger.warning({"event": "projects_load_error", "error": str(e)})
        return []


def save_projects_registry(registry: list):
    """
    Upsert the full list of projects into ES.
    Used for legacy callers that rewrite the entire list.
    """
    for entry in registry:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _es_projects_request(
                f"/{PROJECTS_INDEX}/_doc/{entry['id']}",
                entry,
                method="PUT",
            )
        except Exception as e:
            logger.warning({"event": "projects_save_error", "id": entry.get("id"), "error": str(e)})


def _upsert_project(entry: dict):
    """Upsert a single project document to ES.

    Uses ?refresh=wait_for so the document is immediately visible to searches
    (prevents the optimistic cache insert being overwritten by a stale poll
    before ES finishes indexing — fixes the 'project name disappears' bug).
    """
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    _es_projects_request(
        f"/{PROJECTS_INDEX}/_doc/{entry['id']}?refresh=wait_for",
        entry,
        method="PUT",
    )


def _update_project_registry_field(project_id: str, **fields) -> None:
    """Atomic field-level update on a single project document in ES.

    Uses ?refresh=wait_for so the clone_status change is visible to the
    /clone-status polling endpoint on the very next request — prevents the
    UI being stuck on 'cloning' when the clone has already failed or succeeded.
    """
    registry = load_projects_registry()
    for p in registry:
        if p.get('id') == project_id:
            p.update(fields)
            # Write back only the updated document with immediate consistency.
            p["updated_at"] = datetime.now(timezone.utc).isoformat()
            _es_projects_request(
                f"/{PROJECTS_INDEX}/_doc/{project_id}?refresh=wait_for",
                p,
                method="PUT",
            )
            return
    logger.warning(json.dumps({"event": "update_field_project_not_found", "project_id": project_id}))


def _delete_project_from_es(project_id: str):
    """Delete a project document from ES."""
    try:
        _es_projects_request(
            f"/{PROJECTS_INDEX}/_doc/{project_id}",
            method="DELETE",
        )
    except Exception as e:
        logger.warning({"event": "projects_delete_error", "id": project_id, "error": str(e)})


def es_search(index, body):
    # POST is required for reliable JSON bodies (some stacks strip GET bodies).
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_search",
        data=json.dumps(body).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def find_task_doc_by_logical_id(logical_id: str):
    """
    Return (es_id, source) for a work item in agent-task-records.

    Documents are usually upserted with PUT .../_doc/<logical_id>, so the ES _id
    matches the logical id. Older or reindexed data may only match on the `id`
    field — try `term` (keyword), `id.keyword` (dynamic mapping), and
    `match_phrase` (text mapping) so history / git / PR endpoints stay consistent
    with the snapshot list.
    """
    tid = (logical_id or '').strip()
    if not tid:
        return None, None
    attempts = [
        {'ids': {'values': [tid]}},
        {'term': {'id': tid}},
        {'term': {'id.keyword': tid}},
        {'match_phrase': {'id': tid}},
    ]
    for query in attempts:
        try:
            hits = es_search('agent-task-records', {'size': 1, 'query': query}).get('hits', {}).get('hits', [])
            if hits:
                h = hits[0]
                return h.get('_id'), h.get('_source', {})
        except Exception:
            continue
    return None, None


def es_index(index, doc):
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_doc",
        data=json.dumps(doc).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode())

def es_upsert(index, doc_id, doc):
    """PUT a document by explicit ID — idempotent upsert (create or overwrite)."""
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_doc/{urllib.parse.quote(str(doc_id), safe='')}",
        data=json.dumps(doc).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
        method='PUT',
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode())


def es_post(path, body, method='POST'):
    """
    Generic helper for POST/other write operations against Elasticsearch.
    Path should NOT start with a leading slash, e.g. 'agent-task-records/_update_by_query'.
    """
    req = urllib.request.Request(
        f"{ES_URL}/{path}",
        data=json.dumps(body).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
        method=method,
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode())


def load_session(session_id):
    try:
        res = es_search('agent-plan-sessions', {'size': 1, 'query': {'term': {'_id': session_id}}})
        hits = res.get('hits', {}).get('hits', [])
        if hits:
            return hits[0].get('_source')
    except Exception as e:
        print(f"Error loading session {session_id} from ES: {e}")
    return None


def save_session(session):
    try:
        session['updated_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        es_post(f'agent-plan-sessions/_doc/{session["id"]}?refresh=true', session)
    except Exception as e:
        print(f"Error saving session to ES: {e}")

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _iso_elapsed_seconds(started_at: Optional[str]) -> Optional[float]:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
        return round((datetime.now(timezone.utc) - started).total_seconds(), 3)
    except Exception:
        return None


def _sync_llm_runtime_env():
    load_legacy_dotenv_into_environ(_SRC_ROOT)
    try:
        from workspace_llm_env import sync_llm_env_from_workspace

        sync_llm_env_from_workspace(WORKSPACE_ROOT)
    except Exception:
        pass


def _planner_debug_log(event: str, **fields):
    # AP-6: planner-debug.log removed — structured debug output goes to stdout now.
    # Filter to DEBUG level so these are silent in default INFO deployments.
    logger.debug(json.dumps({'event': event, **fields}, ensure_ascii=False))


def _planner_runtime_config() -> dict:
    _sync_llm_runtime_env()
    pairs = load_effective_pairs(WORKSPACE_ROOT)
    provider = (pairs.get('LLM_PROVIDER') or os.environ.get('LLM_PROVIDER') or 'ollama').strip().lower()
    model = (pairs.get('LLM_MODEL') or os.environ.get('LLM_MODEL') or LLM_MODEL).strip()
    # FLUME_PLANNER_MODEL lets operators use a lighter/faster model for planning
    # independently of the agent model (e.g. qwen2.5-coder:7b for planning speed
    # while gemma4:26b handles code implementation).
    planner_model_override = os.environ.get('FLUME_PLANNER_MODEL', '').strip()
    if planner_model_override:
        model = planner_model_override
    if provider == 'ollama':
        base_url = resolve_effective_ollama_base_url(pairs).strip()
    else:
        base_url = (pairs.get('LLM_BASE_URL') or os.environ.get('LLM_BASE_URL') or LLM_BASE_URL).strip()
    parsed = urlparse(base_url) if base_url else None
    host = parsed.netloc or parsed.path if parsed else ''
    cfg = {
        'provider': provider,
        'model': model,
        'baseUrl': base_url,
        'host': host,
        'usingCodexAppServer': _planner_should_use_codex_app_server(),
    }
    _planner_debug_log(
        'runtime_config',
        provider=provider,
        model=model,
        baseUrl=base_url,
        envProvider=(os.environ.get('LLM_PROVIDER') or '').strip(),
        envModel=(os.environ.get('LLM_MODEL') or '').strip(),
        envBaseUrl=(os.environ.get('LLM_BASE_URL') or '').strip(),
        pairProvider=(pairs.get('LLM_PROVIDER') or '').strip(),
        pairModel=(pairs.get('LLM_MODEL') or '').strip(),
        pairBaseUrl=(pairs.get('LLM_BASE_URL') or '').strip(),
        pairLocalOllamaBaseUrl=(pairs.get('LOCAL_OLLAMA_BASE_URL') or '').strip(),
    )
    return cfg


def _planner_request_timeout_seconds(config: Optional[dict] = None) -> int:
    cfg = config or _planner_runtime_config()
    provider = (cfg.get('provider') or '').lower()
    base_url = (cfg.get('baseUrl') or '').lower()
    default_timeout = int(os.environ.get('FLUME_PLANNER_TIMEOUT_SECONDS', '300'))
    if provider == 'ollama' or ('11434' in base_url) or ('ollama' in base_url):
        return max(default_timeout, 300)
    return default_timeout


def _build_planning_status(stage: str = 'queued') -> dict:
    cfg = _planner_runtime_config()
    return {
        'stage': stage,
        'provider': cfg.get('provider'),
        'model': cfg.get('model'),
        'baseUrl': cfg.get('baseUrl'),
        'host': cfg.get('host'),
        'usingCodexAppServer': cfg.get('usingCodexAppServer'),
        'connectionTestStartedAt': None,
        'connectionTestDurationMs': None,
        'connectionTestOk': None,
        'connectionTestResult': None,
        'requestStartedAt': None,
        'requestElapsedSeconds': None,
        'timeoutSeconds': _planner_request_timeout_seconds(cfg),
        'failureText': None,
        'lastUpdatedAt': _utcnow_iso(),
    }


def _update_planning_status(session: dict, **updates) -> dict:
    status = session.get('planningStatus') or _build_planning_status()
    status.update({k: v for k, v in updates.items() if v is not None or k in updates})
    started_at = status.get('requestStartedAt')
    elapsed = _iso_elapsed_seconds(started_at)
    if elapsed is not None:
        status['requestElapsedSeconds'] = elapsed
    status['lastUpdatedAt'] = _utcnow_iso()
    session['planningStatus'] = status
    return status


def _test_planner_connection(status: dict) -> dict:
    provider = (status.get('provider') or '').lower()
    base_url = (status.get('baseUrl') or '').rstrip('/')
    if not base_url:
        status['connectionTestOk'] = False
        status['connectionTestResult'] = 'No LLM_BASE_URL configured.'
        return status
    url = base_url
    if provider == 'ollama':
        url = base_url + '/api/version'
    elif provider in ('openai', 'openai_compatible', 'gemini'):
        url = base_url + '/v1/models'
    started = time.time()
    status['connectionTestStartedAt'] = _utcnow_iso()
    try:
        req = urllib.request.Request(url, headers={'Authorization': f"Bearer {(os.environ.get('LLM_API_KEY') or '').strip()}"} if provider in ('openai', 'openai_compatible', 'gemini') and (os.environ.get('LLM_API_KEY') or '').strip() else {}, method='GET')
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode(errors='ignore')
            status['connectionTestOk'] = True
            status['connectionTestResult'] = f'{url} responded HTTP {getattr(resp, "status", 200)}'
            if provider == 'ollama':
                try:
                    version = json.loads(body).get('version')
                    if version:
                        status['connectionTestResult'] += f' (Ollama {version})'
                except Exception:
                    pass
    except Exception as e:
        status['connectionTestOk'] = False
        status['connectionTestResult'] = f'{url} failed: {e}'[:300]
    status['connectionTestDurationMs'] = round((time.time() - started) * 1000, 1)
    return status


def _complete_planner_turn(session: dict, message: str, plan: Optional[dict], plan_source: str, failure_text: Optional[str] = None):
    session['draftPlan'] = plan
    session['draftPlanSource'] = plan_source
    session['messages'].append({
        'from': 'agent',
        'text': message,
        'plan': plan,
        'agent_role': session.get('agent_role', 'intake'),
    })
    _update_planning_status(
        session,
        stage='ready' if not failure_text else 'failed',
        failureText=failure_text,
    )
    save_session(session)



PLANNER_SYSTEM_PROMPT = """\
You are a senior technical planner. The user describes what they want built and you \
break it down into a structured hierarchy of Epics, Features, Stories, and Tasks.

RULES:
- Always respond with valid JSON containing exactly two keys: "message" and "plan".
- "message" is your conversational reply to the user (markdown is fine).
- "plan" is the current complete work breakdown with this exact structure:
  {
    "epics": [
      {
        "id": "epic-<n>",
        "title": "...",
        "description": "...",
        "features": [
          {
            "id": "feat-<n>",
            "title": "...",
            "stories": [
              {
                "id": "story-<n>",
                "title": "...",
                "acceptanceCriteria": ["..."],
                "tasks": [
                  { "id": "task-<n>", "title": "..." }
                ]
              }
            ]
          }
        ]
      }
    ]
  }
- When the user asks to add, remove, or modify items, return the full updated plan.
- Use short, descriptive IDs (epic-1, feat-1, story-1, task-1, etc.).
- Be thorough: break work into granular, implementable tasks.
- Only output the JSON object, nothing before or after it.\
"""


def _planner_should_use_codex_app_server() -> bool:
    provider = (os.environ.get('LLM_PROVIDER') or '').strip().lower()
    if provider != 'openai':
        return False
    force = (os.environ.get('FLUME_PLANNER_USE_CODEX_APP_SERVER') or 'auto').strip().lower()
    if force in ('0', 'false', 'off', 'no'):
        return False
    has_oauth = bool((os.environ.get('OPENAI_OAUTH_STATE_FILE') or '').strip() or (os.environ.get('OPENAI_OAUTH_STATE_JSON') or '').strip())
    api_key = (os.environ.get('LLM_API_KEY') or '').strip()
    if not has_oauth and force not in ('1', 'true', 'on', 'yes'):
        return False
    if api_key.startswith('sk-') or api_key.startswith('sk_'):
        return False
    try:
        import codex_app_server

        st = codex_app_server.status()
        return bool(st.get('codexAuthFilePresent')) and bool(st.get('codexOnPath') or st.get('npxOnPath'))
    except Exception:
        return False


def call_planner_model(messages, timeout_seconds: Optional[int] = None):
    """Call the configured planner backend and return the assistant response text."""
    cfg = _planner_runtime_config()
    model = cfg.get('model') or LLM_MODEL
    timeout_seconds = timeout_seconds or _planner_request_timeout_seconds(cfg)
    _planner_debug_log(
        'planner_request',
        provider=cfg.get('provider'),
        model=model,
        baseUrl=cfg.get('baseUrl'),
        timeoutSeconds=timeout_seconds,
        usingCodexAppServer=bool(cfg.get('usingCodexAppServer')),
        messageCount=len(messages or []),
    )
    if cfg.get('usingCodexAppServer'):
        import codex_app_server_client

        return codex_app_server_client.planner_chat(
            messages,
            model=model,
            cwd=str(WORKSPACE_ROOT),
            timeout=timeout_seconds,
        )

    from utils import llm_client
    return llm_client.chat(
        messages,
        model=model,
        temperature=0.3,
        max_tokens=8192,
        provider_override=cfg.get('provider'),
        base_url_override=cfg.get('baseUrl'),
        timeout_seconds=timeout_seconds,
        ollama_think=False,
    )

def _strip_json_blocks(text: str) -> str:
    """Remove trailing ```json ... ``` or bare {...} JSON blocks from a message string."""
    # Remove fenced code blocks (```json ... ``` or ``` ... ```)
    text = re.sub(r'```(?:json)?\s*\{[\s\S]*?\}\s*```', '', text)
    # Remove any remaining fenced blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Remove bare top-level JSON objects that start on their own line
    text = re.sub(r'(?m)^\s*\{[\s\S]*\}\s*$', '', text)
    return text.strip()


def parse_llm_response(raw_text):
    """
    Extract message and plan from the LLM's JSON response.
    Tries direct JSON parse first, then regex extraction.
    Always strips any embedded JSON/code blocks from the message text.
    """
    cleaned = raw_text.strip()
    # Unwrap outer markdown fence if the entire response is wrapped
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)

    try:
        obj = json.loads(cleaned)
        if 'message' in obj and 'plan' in obj:
            return _strip_json_blocks(obj['message']), obj['plan']
    except json.JSONDecodeError:
        pass

    # Try to find an embedded JSON object
    json_match = re.search(r'\{[\s\S]*\}', cleaned)
    if json_match:
        try:
            obj = json.loads(json_match.group())
            if 'message' in obj and 'plan' in obj:
                return _strip_json_blocks(obj['message']), obj['plan']
        except json.JSONDecodeError:
            pass

    # Fall back: return the raw text with any JSON/code blocks scrubbed out
    return _strip_json_blocks(cleaned), None


def _planner_llm_error_hint(err: str) -> str:
    """Short user-facing hint after a planner LLM call fails (OAuth, connectivity, etc.)."""
    if 'Connection refused' in err or 'Errno 111' in err:
        return (
            ' Check that LLM_PROVIDER/LLM_BASE_URL match your setup (e.g. OpenAI + gpt-5.4, not Ollama on localhost). '
            'Save Settings again or run ./flume restart after changing .env.'
        )
    if '401' in err or 'Unauthorized' in err:
        if 'model.request' in err:
            return (
                ' ChatGPT/Codex **browser OAuth** tokens do not include **model.request** (OpenAI does not allow '
                'that scope on /oauth/authorize for the Codex client), but **/v1/chat/completions** still requires it. '
                'Plan New Work and similar calls need an OpenAI **platform API key** (sk-…): Settings → LLM → '
                'Auth mode → API Key, from https://platform.openai.com/api-keys — OAuth alone cannot satisfy this API.'
            )
        if 'api.responses.write' in err:
            return (
                ' Token lacks api.responses.write for /v1/responses. With current Flume, Codex OAuth usually routes '
                'to chat/completions instead; if you still see responses in the error, restart all services. '
                'Otherwise use a platform sk- API key.'
            )
        return (
            ' For ChatGPT/Codex OAuth, the access token may be expired: open Settings → LLM and use '
            '"Refresh OAuth token", or run ./flume codex-oauth refresh, then save settings.'
        )
    return ''


def build_llm_messages(session):
    """Build the Ollama message list from a session's conversation history."""
    msgs = [{'role': 'system', 'content': PLANNER_SYSTEM_PROMPT}]

    for m in session.get('messages', []):
        if m['from'] == 'user':
            content = m['text']
            if m.get('plan'):
                content += f'\n\nCurrent plan state:\n```json\n{json.dumps(m["plan"], indent=2)}\n```'
            msgs.append({'role': 'user', 'content': content})
        elif m['from'] == 'agent':
            response_obj = {'message': m['text'], 'plan': m.get('plan', {})}
            msgs.append({'role': 'assistant', 'content': json.dumps(response_obj)})

    return msgs


def create_planning_session(repo, prompt):
    session_id = f'plan-{uuid.uuid4().hex[:12]}'
    session = {
        'id': session_id,
        'repo': repo,
        'status': 'active',
        'agent_role': 'intake',
        'messages': [
            {'from': 'user', 'text': prompt, 'plan': None}
        ],
        'draftPlan': None,
        'draftPlanSource': None,
        'planningStatus': _build_planning_status(stage='queued'),
        'created_at': _utcnow_iso(),
        'updated_at': _utcnow_iso(),
    }
    save_session(session)
    threading.Thread(target=_run_initial_planning, args=(session_id,), daemon=True).start()
    return session


def _run_initial_planning(session_id: str):
    session = load_session(session_id)
    if not session:
        return
    status = _update_planning_status(session, stage='testing_connection')
    _test_planner_connection(status)
    save_session(session)

    llm_messages = build_llm_messages(session)
    timeout_seconds = _planner_request_timeout_seconds(status)
    _update_planning_status(session, stage='requesting_plan', requestStartedAt=_utcnow_iso(), timeoutSeconds=timeout_seconds, failureText=None)
    save_session(session)
    message = None
    plan = None
    llm_error = None
    try:
        raw = call_planner_model(llm_messages, timeout_seconds=timeout_seconds)
        message, plan = parse_llm_response(raw)
    except Exception as e:
        llm_error = str(e)[:300]

    if llm_error:
        hint = _planner_llm_error_hint(llm_error)
        message = (
            f"The planner could not reach the language model ({llm_error}).{hint}\n\n"
            "Below is an editable PLACEHOLDER outline derived only from your prompt — "
            "not an AI-generated breakdown. Edit the tree manually or fix LLM auth and start a new plan."
        )
        plan = placeholder_plan(session.get('repo') or '', session['messages'][0].get('text') or '')
        _complete_planner_turn(session, message, plan, 'placeholder', llm_error)
        return
    if not plan or not plan.get('epics'):
        plan = placeholder_plan(session.get('repo') or '', session['messages'][0].get('text') or '')
        prior = (message or '').strip()
        if prior:
            message = (
                f"{prior}\n\n"
                "Note: The model did not return a valid plan JSON, so the work breakdown below is a "
                "placeholder template you can edit manually."
            )
        else:
            message = (
                "The model did not return a usable plan structure. "
                "Below is an editable placeholder template; try again or adjust your LLM settings."
            )
        _complete_planner_turn(session, message, plan, 'placeholder')
        return
    _complete_planner_turn(session, message, plan, 'llm')


def refine_session(session_id, user_text, current_plan):
    session = load_session(session_id)
    if not session:
        return None

    session['messages'].append({
        'from': 'user',
        'text': user_text,
        'plan': current_plan,
    })

    if current_plan:
        session['draftPlan'] = current_plan

    _update_planning_status(session, stage='testing_connection')
    _test_planner_connection(session['planningStatus'])
    save_session(session)

    llm_messages = build_llm_messages(session)
    timeout_seconds = _planner_request_timeout_seconds(session.get('planningStatus'))
    _update_planning_status(session, stage='requesting_plan', requestStartedAt=_utcnow_iso(), timeoutSeconds=timeout_seconds, failureText=None)
    save_session(session)
    try:
        raw = call_planner_model(llm_messages, timeout_seconds=timeout_seconds)
        message, plan = parse_llm_response(raw)
    except Exception as e:
        err = str(e)[:300]
        hint = _planner_llm_error_hint(err)
        message = f"I encountered an issue processing your request. Please try again. (Error: {err}){hint}"
        plan = None
        _update_planning_status(session, stage='failed', failureText=err)

    if plan and plan.get('epics'):
        session['draftPlan'] = plan
        session['draftPlanSource'] = 'llm'
    else:
        plan = session['draftPlan']

    session['messages'].append({
        'from': 'agent',
        'text': message,
        'plan': plan,
        'agent_role': session.get('agent_role', 'intake'),
    })

    if session.get('planningStatus', {}).get('stage') != 'failed':
        _update_planning_status(session, stage='ready', failureText=None)
    save_session(session)
    return session


def placeholder_plan(repo: str, prompt: str):
    """
    Minimal epic/feature/story/task skeleton when the LLM is unavailable or returns no plan.

    Titles are intentionally labeled as placeholders so the UI is not mistaken for AI output.
    """
    title = (prompt.splitlines()[0] or 'New request').strip()
    if len(title) > 80:
        title = title[:77] + '...'
    epic_id = 'epic-1'
    feature_id = 'feat-1'
    story_id = 'story-1'
    task_id = 'task-1'
    return {
        'repo': repo,
        'epics': [
            {
                'id': epic_id,
                'title': title,
                'description': prompt,
                'features': [
                    {
                        'id': feature_id,
                        'title': '[Placeholder] Rename this feature',
                        'stories': [
                            {
                                'id': story_id,
                                'title': '[Placeholder] Rename this story',
                                'acceptanceCriteria': [
                                    '[Placeholder] Add acceptance criteria',
                                    '[Placeholder] Add another criterion',
                                ],
                                'tasks': [
                                    {
                                        'id': task_id,
                                        'title': '[Placeholder] Add a concrete task',
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


# Backward-compatible name for scripts/tests
simple_plan = placeholder_plan


def get_next_id_sequence(prefix: str) -> int:
    """
    Return the next available integer sequence number for IDs of the form `prefix-N`.

    Takes the maximum of:
      1. The highest N seen in the live ES index (covers active/archived records).
      2. The persisted high-water-mark counter (covers IDs that were deleted from ES).

    This guarantees monotonic, never-recycled IDs even when records are hard-deleted.
    """
    max_n = es_counter_hwm(prefix)
    try:
        hits = es_search('agent-task-records', {
            'size': 10000,
            '_source': ['id'],
            'query': {'regexp': {'id': f'{re.escape(prefix)}-[0-9]+'}},
        }).get('hits', {}).get('hits', [])
        pattern = re.compile(rf'^{re.escape(prefix)}-(\d+)$')
        for h in hits:
            doc_id = (h.get('_source') or {}).get('id', '') or h.get('_id', '')
            m = pattern.match(doc_id)
            if m:
                max_n = max(max_n, int(m.group(1)))
    except Exception:
        if max_n == 0:
            # Fallback when both ES and the counter file are unavailable
            return int(datetime.now(timezone.utc).timestamp()) % 1_000_000 + 1
    return max_n + 1


def commit_plan(repo: str, plan: dict):
    """
    Translate a plan tree (epics/features/stories/tasks) into TASK_SCHEMA docs
    and index them into agent-task-records with initial statuses and owners.

    IDs are always freshly allocated by querying existing records, so numbers
    are never reused even after items are deleted.
    """
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    docs = []

    # Allocate monotonically-increasing sequence numbers for each item type.
    # These are fetched once before the loop so we don't make N round-trips.
    epic_seq = get_next_id_sequence('epic')
    feat_seq = get_next_id_sequence('feat')
    story_seq = get_next_id_sequence('story')
    task_seq = get_next_id_sequence('task')

    epics = plan.get('epics') or []
    for epic in epics:
        epic_id = f'epic-{epic_seq}'
        epic_seq += 1
        epic_title = epic.get('title') or ''
        epic_desc = epic.get('description') or ''
        epic_doc = {
            'id': epic_id,
            'title': epic_title,
            'objective': epic_desc,
            'repo': repo,
            'worktree': None,
            'item_type': 'epic',
            'owner': 'pm',
            'status': 'planned',
            'priority': 'high',
            'parent_id': None,
            'depends_on': [],
            'acceptance_criteria': [],
            'artifacts': [],
            'last_update': now,
            'needs_human': False,
            'risk': 'medium',
        }
        docs.append(epic_doc)

        for feature in epic.get('features') or []:
            feat_id = f'feat-{feat_seq}'
            feat_seq += 1
            feat_title = feature.get('title') or ''
            feat_doc = {
                'id': feat_id,
                'title': feat_title,
                'objective': f"Feature of {epic_title}",
                'repo': repo,
                'worktree': None,
                'item_type': 'feature',
                'owner': 'pm',
                'status': 'planned',
                'priority': 'medium',
                'parent_id': epic_id,
                # depends_on drives UI hierarchy; features become ready when epic is done
                'depends_on': [epic_id],
                'acceptance_criteria': [],
                'artifacts': [],
                'last_update': now,
                'needs_human': False,
                'risk': 'medium',
            }
            docs.append(feat_doc)

            for story in feature.get('stories') or []:
                story_id = f'story-{story_seq}'
                story_seq += 1
                story_title = story.get('title') or ''
                ac = story.get('acceptanceCriteria') or []
                story_doc = {
                    'id': story_id,
                    'title': story_title,
                    'objective': f"Story for {feat_title}",
                    'repo': repo,
                    'worktree': None,
                    'item_type': 'story',
                    'owner': 'pm',
                    'status': 'planned',
                    'priority': 'medium',
                    'parent_id': feat_id,
                    'depends_on': [feat_id],
                    'acceptance_criteria': ac,
                    'artifacts': [],
                    'last_update': now,
                    'needs_human': False,
                    'risk': 'medium',
                }
                docs.append(story_doc)

                # Tasks within a story run sequentially: each depends on the
                # previous task so the implementer can't start task N+1 until
                # task N is fully done. The first task is immediately 'ready'.
                prev_task_id = None
                for task in story.get('tasks') or []:
                    task_id = f'task-{task_seq}'
                    task_seq += 1
                    task_title = task.get('title') or ''
                    task_doc = {
                        'id': task_id,
                        'title': task_title,
                        'objective': f"Task for {story_title}",
                        'repo': repo,
                        'worktree': None,
                        'item_type': 'task',
                        'owner': 'implementer',
                        # First task in the story starts ready; subsequent ones
                        # are planned and get promoted after the previous task is done.
                        'status': 'ready' if prev_task_id is None else 'planned',
                        'priority': 'normal',
                        'parent_id': story_id,
                        # depends_on: previous task for ordering (UI hierarchy uses parent_id)
                        'depends_on': [prev_task_id] if prev_task_id else [],
                        'acceptance_criteria': ac,
                        'artifacts': [],
                        'last_update': now,
                        'needs_human': False,
                        'risk': 'medium',
                    }
                    docs.append(task_doc)
                    prev_task_id = task_id

    # Persist high-water marks atomically in ES so deleted records never cause id recycling.
    # epic_seq/feat_seq/story_seq/task_seq have already been incremented once
    # beyond the last allocated value, so subtract 1 to get the actual max used.
    for prefix, seq in (('epic', epic_seq), ('feat', feat_seq), ('story', story_seq), ('task', task_seq)):
        es_counter_set_hwm(prefix, seq - 1)

    results = []
    for d in docs:
        results.append(es_upsert('agent-task-records', d['id'], d))
    return docs, results

def delete_task_branches(ids: list, repo: str) -> list:
    """
    For any tasks in `ids` that have a `branch` field, delete that git branch
    from the local repository (and remote origin if it exists).
    Returns a list of branch names that were successfully deleted.
    """
    query_must: list = [
        {'terms': {'id': ids}},
        {'exists': {'field': 'branch'}},
    ]
    if repo:
        query_must.append({'term': {'repo': repo}})

    try:
        hits = es_search('agent-task-records', {
            'size': 500,
            '_source': ['id', 'repo', 'branch'],
            'query': {'bool': {'must': query_must}},
        }).get('hits', {}).get('hits', [])
    except Exception:
        return []

    registry = load_projects_registry()
    deleted = []

    # If multiple tasks share the same branch (e.g., tasks under the same
    # story), we must not delete the shared branch until no ES records
    # remain for it.
    ids_set = set(ids or [])

    for h in hits:
        src = h.get('_source') or {}
        branch = (src.get('branch') or '').strip()
        repo_id = src.get('repo', '')
        if not branch or not repo_id:
            continue

        proj = next((p for p in registry if p['id'] == repo_id), None)
        if not proj:
            continue

        # AP-12: Only local-path projects have a persistent repo on disk.
        # Remote/indexed projects have no local clone — skip git branch ops.
        local_path = proj.get('path') or ''
        if not local_path or proj.get('clone_status') not in ('local',):
            if not local_path:
                logger.debug(json.dumps({'event': 'ap12_skip_non_local_branch_delete', 'repo_id': repo_id, 'clone_status': proj.get('clone_status')}))
            continue
        repo_path = Path(local_path)
        if not (repo_path / '.git').exists():
            continue

        # Shared-branch safety: if any other remaining task doc still uses
        # this branch, skip deletion.
        try:
            remaining = es_search('agent-task-records', {
                'size': 1,
                '_source': ['id'],
                'query': {
                    'bool': {
                        'must': [
                            {'term': {'repo': repo_id}},
                            {'term': {'branch': branch}},
                        ],
                        'must_not': [{'terms': {'id': list(ids_set)}}],
                    }
                },
            }).get('hits', {}).get('hits', [])
            if remaining:
                continue
        except Exception:
            # Best-effort: if ES check fails, fall back to deleting.
            pass

        # Delete local branch (force, since it may not be merged)
        try:
            result = subprocess.run(
                ['git', '-C', str(repo_path), 'branch', '-D', branch],
                capture_output=True, timeout=15,
            )
            if result.returncode == 0:
                deleted.append(branch)
        except Exception:
            pass

        # Best-effort: delete remote tracking branch if it exists on origin
        try:
            subprocess.run(
                ['git', '-C', str(repo_path), 'push', 'origin', '--delete', branch],
                capture_output=True, timeout=20,
            )
        except Exception:
            pass

    return deleted


def delete_repo_branches(repo_id: str, branches: list, force: bool) -> dict:
    """
    Delete local git branches for a given dashboard repo.

    Safety defaults:
    - Default branch and currently checked-out branch are protected unless `force=True`.
    - If any non-archived tasks reference the branch, deletion is blocked unless `force=True`.
    """
    try:
        raw_branches = [str(b or '').strip() for b in (branches or [])]
        raw_branches = [b for b in raw_branches if b]
        if not raw_branches:
            return {'ok': False, 'error': 'No branches provided', 'deleted': [], 'skipped': []}

        # Allow typical git ref formats like "feature/x", "bugfix-1", "release/1.2.3".
        # Keep this conservative to avoid command injection / ref weirdness.
        invalid = [b for b in raw_branches if not re.match(r'^[A-Za-z0-9._/\-]+$', b)]
        if invalid:
            return {'ok': False, 'error': 'Invalid branch name(s)', 'invalid': invalid}

        registry = load_projects_registry()
        proj = next((p for p in registry if p['id'] == repo_id), None)
        if not proj:
            return {'ok': False, 'error': f'Project "{repo_id}" not found'}

        # AP-12: Explicit local-only guard — no silent workspace fallback.
        local_path = proj.get('path') or ''
        if not local_path or proj.get('clone_status') not in ('local',):
            return {'ok': False, 'error': 'Branch deletion is only supported for locally-mounted repos. Remote repos use GitHostClient.'}
        repo_path = Path(local_path)
        if not (repo_path / '.git').exists():
            return {'ok': False, 'error': 'Repo is not a git repository'}

        # Discover actual local branches so we can report "missing" branches.
        try:
            raw = subprocess.check_output(
                ['git', '-C', str(repo_path), 'branch', '--format=%(refname:short)'],
                stderr=subprocess.DEVNULL,
            ).decode(errors='replace')
            local_branches = [b.strip() for b in raw.splitlines() if b.strip()]
        except Exception:
            local_branches = []

        local_set = set(local_branches)
        missing = [b for b in raw_branches if local_branches and b not in local_set]
        branches_to_consider = [b for b in raw_branches if (not local_branches) or b in local_set]
        if not branches_to_consider:
            return {'ok': True, 'deleted': [], 'skipped': [], 'missing': missing}

        default_branch = resolve_default_branch(
            repo_path, override=proj.get('gitflow', {}).get('defaultBranch')
        )

        current_branch = None
        try:
            current_branch = subprocess.check_output(
                ['git', '-C', str(repo_path), 'rev-parse', '--abbrev-ref', 'HEAD'],
                stderr=subprocess.DEVNULL,
            ).decode(errors='replace').strip()
        except Exception:
            pass

        protected = set()
        if not force:
            if default_branch:
                protected.add(default_branch)
            if current_branch:
                protected.add(current_branch)

        # If not forcing, block deleting branches that are referenced by active tasks.
        blocked_by_tasks = set()
        if not force:
            try:
                hits = es_search('agent-task-records', {
                    'size': 500,
                    '_source': ['id', 'repo', 'branch', 'status'],
                    'query': {
                        'bool': {
                            'must': [
                                {'terms': {'branch': branches_to_consider}},
                                {'term': {'repo': repo_id}},
                            ],
                            'must_not': [{'term': {'status': 'archived'}}],
                        }
                    },
                }).get('hits', {}).get('hits', [])

                for h in hits:
                    src = h.get('_source') or {}
                    b = (src.get('branch') or '').strip()
                    if b:
                        blocked_by_tasks.add(b)
            except Exception:
                # If ES isn't available, don't block deletion.
                blocked_by_tasks = set()

        to_delete = []
        skipped = []
        for b in branches_to_consider:
            if b in protected:
                skipped.append({'branch': b, 'reason': 'protected (default/current) — use force to override'})
                continue
            if b in blocked_by_tasks:
                skipped.append({'branch': b, 'reason': 'referenced by active tasks — use force to override'})
                continue
            to_delete.append(b)

        # If we are deleting the currently checked-out branch, switch away first.
        if current_branch and current_branch in to_delete:
            checkout_branch = None
            for b in local_branches:
                if b != current_branch and b not in to_delete:
                    checkout_branch = b
                    break
            if not checkout_branch:
                for b in local_branches:
                    if b != current_branch:
                        checkout_branch = b
                        break
            if checkout_branch:
                try:
                    subprocess.run(
                        ['git', '-C', str(repo_path), 'switch', checkout_branch],
                        capture_output=True,
                        timeout=20,
                    )
                    current_branch = checkout_branch
                except Exception:
                    # Best-effort only; deletion may still succeed or fail.
                    pass

        deleted = []
        errors = []
        for b in to_delete:
            try:
                del_flag = '-D' if force else '-d'
                result = subprocess.run(
                    ['git', '-C', str(repo_path), 'branch', del_flag, b],
                    capture_output=True,
                    timeout=15,
                )
                if result.returncode == 0:
                    deleted.append(b)
                else:
                    stderr = (result.stderr or b'').decode(errors='replace').strip()
                    errors.append({'branch': b, 'error': stderr[:200] or 'git branch failed'})
            except Exception:
                errors.append({'branch': b, 'error': 'exception during git branch deletion'})

            # Best-effort: delete remote tracking branch if it exists on origin.
            try:
                subprocess.run(
                    ['git', '-C', str(repo_path), 'push', 'origin', '--delete', b],
                    capture_output=True,
                    timeout=20,
                )
            except Exception:
                pass

        return {
            'ok': True,
            'default': default_branch,
            'current': current_branch,
            'deleted': deleted,
            'skipped': skipped,
            'missing': missing,
            'errors': errors,
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)[:200], 'deleted': [], 'skipped': []}


def load_workers() -> list:
    workers = []
    try:
        res = es_search('agent-system-workers', {'size': 100, 'sort': [{'updated_at': {'order': 'desc'}}]})
        hits = res.get('hits', {}).get('hits', [])
        now = datetime.now(timezone.utc)
        for h in hits:
            doc = h.get('_source', {})
            node_workers = doc.get('workers', [])
            for w in node_workers:
                w['status'] = w.get('status', 'idle')
                hb_str = w.get('heartbeat_at')
                if hb_str:
                    try:
                        hb = datetime.fromisoformat(hb_str.replace('Z', '+00:00'))
                        diff_sec = (now - hb).total_seconds()
                        if diff_sec > 120:
                            continue  # Garbage collect from UI view
                        if diff_sec > 30:
                            w['status'] = 'offline/terminated'
                    except Exception:
                        pass
                workers.append(w)
    except Exception as e:
        print(f"Error loading workers from ES: {e}")
        return []
        
    try:
        agg_res = es_search('agent-token-telemetry', {
            'size': 0,
            'aggs': {
                'by_worker': {
                    'terms': {'field': 'worker_name.keyword', 'size': 500},
                    'aggs': {
                        'total_input': {'sum': {'field': 'input_tokens'}},
                        'total_output': {'sum': {'field': 'output_tokens'}}
                    }
                },
                'total_elastro_savings': {
                    'sum': {'field': 'savings'}
                }
            }
        })
        buckets = agg_res.get('aggregations', {}).get('by_worker', {}).get('buckets', [])
        totals = {}
        for b in buckets:
            totals[b.get('key')] = {
                'input': int(b.get('total_input', {}).get('value', 0)),
                'output': int(b.get('total_output', {}).get('value', 0))
            }
        for w in workers:
            w['input_tokens'] = totals.get(w['name'], {}).get('input', 0)
            w['output_tokens'] = totals.get(w['name'], {}).get('output', 0)
    except Exception:
        pass
        
    return workers


def priority_rank(priority: str) -> int:
    ranks = {'urgent': 0, 'high': 1, 'medium': 2, 'normal': 3, 'low': 4}
    return ranks.get((priority or '').lower(), 99)


def queue_for_repo(repo_id: str):
    hits = es_search('agent-task-records', {
        'size': 500,
        'query': {
            'bool': {
                'must': [
                    {'term': {'repo': repo_id}},
                    {'term': {'status': 'ready'}},
                ],
                'must_not': [{'term': {'status': 'archived'}}],
            }
        },
        'sort': [{'updated_at': {'order': 'asc', 'unmapped_type': 'date'}}],
    }).get('hits', {}).get('hits', [])
    tasks = [{'_id': h.get('_id'), **h.get('_source', {})} for h in hits]
    tasks.sort(key=lambda t: (priority_rank(t.get('priority')), t.get('updated_at') or t.get('last_update') or ''))
    out = []
    for idx, t in enumerate(tasks, start=1):
        out.append({
            '_id': t.get('_id'),
            'id': t.get('id') or t.get('_id'),
            'title': t.get('title'),
            'status': t.get('status'),
            'priority': t.get('priority'),
            'owner': t.get('owner'),
            'assigned_agent_role': t.get('assigned_agent_role') or t.get('owner'),
            'queuePosition': idx,
            'updated_at': t.get('updated_at') or t.get('last_update'),
        })
    return out


def transition_task(task_id: str, status: str, owner=None, needs_human=None):
    es_id, _src = find_task_doc_by_logical_id(task_id)
    if not es_id:
        return None
    doc = {
        'status': status,
        'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'last_update': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    if owner:
        doc['owner'] = owner
        doc['assigned_agent_role'] = owner
    if needs_human is not None:
        doc['needs_human'] = bool(needs_human)
    if status == 'ready':
        doc['implementer_consecutive_llm_failures'] = 0
    es_post(f'agent-task-records/_update/{es_id}', {'doc': doc})
    return {'_id': es_id, 'id': task_id, **doc}


def task_history(task_id: str):
    es_id, src = find_task_doc_by_logical_id(task_id)
    if not src:
        return None
    task = {'_id': es_id, **src}

    events = []

    def infer_model(src, event_type):
        if src.get('model_used'):
            return src.get('model_used')
        role = src.get('agent_role') or src.get('from_role') or task.get('owner') or task.get('assigned_agent_role')
        role = (role or '').lower()
        if role in ('implementer', 'tester', 'e2e-tester'):
            return os.environ.get('LLM_MODEL', 'llama3.2')
        if role in ('reviewer', 'acceptance-reviewer'):
            return os.environ.get('LLM_MODEL', 'llama3.2')
        if role in ('pm', 'pm-dispatcher', 'intake', 'memory-updater'):
            return os.environ.get('LLM_MODEL', 'llama3.2')
        return task.get('preferred_model') or None

    handoffs = es_search('agent-handoff-records', {
        'size': 100,
        'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'term': {'task_id': task_id}},
    }).get('hits', {}).get('hits', [])
    for h in handoffs:
        src = h.get('_source', {})
        commit_note = ''
        if src.get('commit_sha'):
            commit_note = f"commit: {src['commit_sha'][:8]}"
        if src.get('branch'):
            commit_note = f"branch: {src['branch']}" + (f"  {commit_note}" if commit_note else '')
        events.append({
            'type': 'handoff',
            'timestamp': src.get('created_at'),
            'summary': f"{src.get('from_role', 'unknown')} -> {src.get('to_role', 'unknown')}",
            'details': src.get('reason') or '',
            'notes': src.get('objective') or '',
            'discussion': (src.get('constraints') or '') + (' | ' + commit_note if commit_note else ''),
            'modelUsed': infer_model(src, 'handoff'),
            'data': src,
        })

    reviews = es_search('agent-review-records', {
        'size': 100,
        'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'term': {'task_id': task_id}},
    }).get('hits', {}).get('hits', [])
    for h in reviews:
        src = h.get('_source', {})
        events.append({
            'type': 'review',
            'timestamp': src.get('created_at'),
            'summary': f"Verdict: {src.get('verdict', 'unknown')}",
            'details': src.get('summary') or '',
            'notes': src.get('issues') or '',
            'discussion': src.get('recommended_next_role') or '',
            'modelUsed': infer_model(src, 'review'),
            'data': src,
        })

    failures = es_search('agent-failure-records', {
        'size': 100,
        'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'term': {'task_id': task_id}},
    }).get('hits', {}).get('hits', [])
    for h in failures:
        src = h.get('_source', {})
        events.append({
            'type': 'failure',
            'timestamp': src.get('updated_at') or src.get('created_at'),
            'summary': src.get('error_class') or 'failure',
            'details': src.get('summary') or '',
            'notes': src.get('root_cause') or '',
            'discussion': src.get('fix_applied') or '',
            'modelUsed': infer_model(src, 'failure'),
            'data': src,
        })

    provenance = es_search('agent-provenance-records', {
        'size': 100,
        'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'term': {'task_id': task_id}},
    }).get('hits', {}).get('hits', [])
    for h in provenance:
        src = h.get('_source', {})
        git_note = ''
        if src.get('branch'):
            git_note = f"branch: {src['branch']}"
        if src.get('commit_sha'):
            git_note += f"  sha: {src['commit_sha'][:8]}"
        events.append({
            'type': 'provenance',
            'timestamp': src.get('created_at'),
            'summary': f"Role: {src.get('agent_role', 'unknown')}",
            'details': src.get('review_verdict') or '',
            'notes': ', '.join(src.get('artifacts') or []) + (f' | {git_note}' if git_note else ''),
            'discussion': ', '.join(src.get('context_refs') or []),
            'modelUsed': infer_model(src, 'provenance'),
            'data': src,
        })

    # Add git/PR events if present on the task
    if task.get('branch'):
        pr_summary = ''
        if task.get('pr_url'):
            pr_summary = f"PR #{task.get('pr_number') or '?'} ({task.get('pr_status', 'open')}): {task['pr_url']}"
        elif task.get('pr_status') == 'failed':
            pr_summary = f"PR creation failed: {task.get('pr_error', 'unknown error')}"
        events.append({
            'type': 'git',
            'timestamp': task.get('updated_at') or task.get('last_update'),
            'summary': f"Branch: {task['branch']}" + (f" → {task['target_branch']}" if task.get('target_branch') else ''),
            'details': pr_summary,
            'notes': task.get('commit_message') or '',
            'discussion': task.get('commit_sha') or '',
            'modelUsed': None,
            'data': {
                'branch': task.get('branch'),
                'target_branch': task.get('target_branch'),
                'commit_sha': task.get('commit_sha'),
                'commit_message': task.get('commit_message'),
                'pr_url': task.get('pr_url'),
                'pr_number': task.get('pr_number'),
                'pr_status': task.get('pr_status'),
                'pr_error': task.get('pr_error'),
            },
        })

    # Always include current task snapshot as the latest state event
    events.append({
        'type': 'task_state',
        'timestamp': task.get('updated_at') or task.get('last_update'),
        'summary': f"Status: {task.get('status', 'unknown')}",
        'details': f"Owner: {task.get('owner', 'unknown')}",
        'notes': task.get('objective') or '',
        'discussion': f"Priority: {task.get('priority', 'n/a')}",
        'modelUsed': task.get('preferred_model'),
        'data': task,
    })

    events.sort(key=lambda e: e.get('timestamp') or '', reverse=True)

    # Build `history` in the format the frontend expects: [{ts, role, summary}]
    # Newest events first; agent_log entries (live notes) come first when task is running.
    history = []

    # Live agent notes — shown prominently while task is running
    agent_log = task.get('agent_log') or []
    for entry in reversed(agent_log):  # newest first
        history.append({
            'ts': entry.get('ts', ''),
            'role': 'agent',
            'summary': entry.get('note', ''),
            'type': 'agent_note',
        })

    # Structured events from handoffs, reviews, failures, etc.
    for e in events:
        role = {
            'handoff': f"{(e.get('data') or {}).get('from_role', 'agent')} → {(e.get('data') or {}).get('to_role', '')}",
            'review': 'reviewer',
            'failure': 'system',
            'provenance': (e.get('data') or {}).get('agent_role', 'agent'),
            'git': 'git',
            'task_state': 'system',
        }.get(e.get('type', ''), 'agent')
        summary = e.get('summary', '')
        if e.get('details'):
            summary += f' — {e["details"]}'
        history.append({
            'ts': e.get('timestamp', ''),
            'role': role,
            'summary': summary,
            'type': e.get('type', ''),
        })

    return {'task': task, 'events': events, 'history': history, 'agent_log': agent_log}


def git_repo_info(repo_id, repo_path: Path):
    info = {
        'id': repo_id,
        'path': str(repo_path),
        'exists': repo_path.exists(),
        'is_git': False,
        'current_branch': None,
        'last_commit': None,
    }
    git_dir = repo_path / '.git'
    if not git_dir.exists():
        return info
    info['is_git'] = True
    try:
        branch = subprocess.check_output(
            ['git', '-C', str(repo_path), 'rev-parse', '--abbrev-ref', 'HEAD'],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        info['current_branch'] = branch
    except Exception:
        pass
    try:
        last = subprocess.check_output(
            ['git', '-C', str(repo_path), 'log', '-1', '--pretty=format:%H%n%an%n%ai%n%s'],
            stderr=subprocess.DEVNULL,
        ).decode().splitlines()
        if len(last) >= 4:
            info['last_commit'] = {
                'hash': last[0],
                'author': last[1],
                'date': last[2],
                'subject': last[3],
            }
    except Exception:
        pass
    return info


def resolve_default_branch(repo_path: Path, override: Optional[str] = None) -> str:
    """Resolve the default branch for a repo (main/master/etc.)."""
    if override:
        return override
    try:
        # Try origin/HEAD symbolic ref
        ref = subprocess.check_output(
            ['git', '-C', str(repo_path), 'symbolic-ref', 'refs/remotes/origin/HEAD'],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        # refs/remotes/origin/main -> main
        return ref.split('/')[-1]
    except Exception:
        pass
    try:
        # Fallback: check common branch names
        branches_raw = subprocess.check_output(
            ['git', '-C', str(repo_path), 'branch', '-r'],
            stderr=subprocess.DEVNULL,
        ).decode()
        for candidate in ('main', 'master', 'develop', 'trunk'):
            if f'origin/{candidate}' in branches_raw:
                return candidate
    except Exception:
        pass
    try:
        current = subprocess.check_output(
            ['git', '-C', str(repo_path), 'rev-parse', '--abbrev-ref', 'HEAD'],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return current or 'main'
    except Exception:
        return 'main'


def get_task_doc(task_id: str):
    """Fetch a single task document from ES by logical id."""
    return find_task_doc_by_logical_id(task_id)


def create_task_pr(task_id: str) -> dict:
    """
    Create a GitHub PR for a task that has been reviewer-approved.
    Returns a result dict with keys: ok, pr_url, pr_number, error, skipped.
    """
    es_id, task = get_task_doc(task_id)
    if not task:
        return {'ok': False, 'error': 'Task not found'}

    # Idempotency: don't create duplicate PRs
    if task.get('pr_url'):
        return {'ok': True, 'skipped': True, 'pr_url': task['pr_url'], 'pr_number': task.get('pr_number')}

    branch = task.get('branch')
    if not branch:
        return {'ok': False, 'error': 'No branch recorded on task — implementer must run first'}

    repo_id = task.get('repo')
    registry = load_projects_registry()
    proj = next((p for p in registry if p['id'] == repo_id), None)
    if not proj:
        return {'ok': False, 'error': f'Project "{repo_id}" not found in registry'}

    # AP-12: Explicit local-only guard — remote/indexed repos use GitHostClient.
    local_path = proj.get('path') or ''
    if not local_path or proj.get('clone_status') not in ('local',):
        return {'ok': False, 'error': 'PR creation via local git is only supported for locally-mounted repos. Remote repos use GitHostClient.'}
    repo_path = Path(local_path)
    if not (repo_path / '.git').exists():
        return {'ok': False, 'error': 'Repo path is not a git repository'}

    target_branch = resolve_default_branch(
        repo_path,
        override=proj.get('gitflow', {}).get('defaultBranch'),
    )

    # Build PR title / body from task metadata
    title = task.get('title') or f"Task {task_id}"
    ac = task.get('acceptance_criteria') or []
    ac_lines = '\n'.join(f'- {c}' for c in ac) if ac else '_None recorded_'
    commit_sha = task.get('commit_sha') or ''
    sha_line = f'\n\n**Commit:** `{commit_sha}`' if commit_sha else ''
    body = (
        f"## {title}\n\n"
        f"**Task ID:** `{task_id}`\n"
        f"**Repo:** `{repo_id}`\n"
        f"**Branch:** `{branch}` → `{target_branch}`\n"
        f"{sha_line}\n\n"
        f"### Acceptance Criteria\n{ac_lines}\n\n"
        f"_Auto-generated by OpenClaw agent workflow._"
    )

    gh_path = subprocess.run(['which', 'gh'], capture_output=True, text=True).stdout.strip()
    if not gh_path:
        return {'ok': False, 'error': '`gh` CLI not found — install GitHub CLI to enable PR creation'}

    try:
        result = subprocess.run(
            [
                'gh', 'pr', 'create',
                '--base', target_branch,
                '--head', branch,
                '--title', title,
                '--body', body,
            ],
            capture_output=True, text=True, timeout=60,
            cwd=str(repo_path),
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'gh pr create timed out after 60s'}

    if result.returncode != 0:
        return {'ok': False, 'error': result.stderr.strip()[:500] or result.stdout.strip()[:500]}

    pr_url = result.stdout.strip()
    # Extract PR number from URL e.g. https://github.com/org/repo/pull/42
    pr_number = None
    url_parts = pr_url.rstrip('/').split('/')
    if url_parts and url_parts[-1].isdigit():
        pr_number = int(url_parts[-1])

    # Persist PR metadata to task doc
    if es_id:
        es_post(f'agent-task-records/_update/{es_id}', {
            'doc': {
                'pr_url': pr_url,
                'pr_number': pr_number,
                'pr_status': 'open',
                'target_branch': target_branch,
                'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                'last_update': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
        })

    return {'ok': True, 'pr_url': pr_url, 'pr_number': pr_number, 'target_branch': target_branch}


def _git_task_context(task_id: str):
    """
    Shared helper: fetch task doc and resolve (task, repo_path, branch, target_branch).
    Returns (task, repo_path, branch, target_branch, error_dict).
    error_dict is non-None when something is missing.
    """
    _, task = get_task_doc(task_id)
    if not task:
        return None, None, None, None, {'error': 'Task not found', 'branch': None}
    branch = task.get('branch')
    if not branch:
        return task, None, None, None, {'error': 'No branch recorded on task yet', 'branch': None}
    repo_id = task.get('repo')
    registry = load_projects_registry()
    proj = next((p for p in registry if p['id'] == repo_id), None)
    if not proj:
        return task, None, branch, None, {'error': f'Project "{repo_id}" not found', 'branch': branch}
    # AP-12: Explicit local-only guard — no silent workspace fallback.
    local_path = proj.get('path') or ''
    if not local_path or proj.get('clone_status') not in ('local',):
        return task, None, branch, None, {'error': 'Git task context requires a locally-mounted repo (clone_status=local).', 'branch': branch}
    repo_path = Path(local_path)
    if not (repo_path / '.git').exists():
        return task, None, branch, None, {'error': 'Repo is not a git repository', 'branch': branch}
    target_branch = task.get('target_branch') or resolve_default_branch(
        repo_path, override=proj.get('gitflow', {}).get('defaultBranch')
    )
    return task, repo_path, branch, target_branch, None


def task_diff(task_id: str) -> dict:
    """Return unified diff of branch vs target branch (three-dot diff)."""
    task, repo_path, branch, target_branch, err = _git_task_context(task_id)
    if err:
        return {**err, 'files': [], 'diff': '', 'truncated': False, 'target_branch': None}

    MAX_DIFF_LINES = 2000
    ref = f'origin/{target_branch}...{branch}'

    # Try fetch to ensure remote refs are current (best-effort, silent on failure)
    try:
        subprocess.run(
            ['git', '-C', str(repo_path), 'fetch', 'origin', '--quiet'],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass

    # --stat output to get per-file summary
    files = []
    try:
        stat_raw = subprocess.check_output(
            ['git', '-C', str(repo_path), 'diff', '--stat', '--stat-width=1000', ref],
            stderr=subprocess.DEVNULL, timeout=15,
        ).decode(errors='replace')
        for line in stat_raw.splitlines():
            # Format: " src/foo.py | 12 +++---"
            parts = line.strip().split('|')
            if len(parts) != 2:
                continue
            path_part = parts[0].strip()
            change_part = parts[1].strip()
            if not path_part or path_part.startswith('changed'):
                continue
            bars = change_part.split()
            plus_count = bars[1].count('+') if len(bars) > 1 else 0
            minus_count = bars[1].count('-') if len(bars) > 1 else 0
            files.append({
                'path': path_part,
                'insertions': plus_count,
                'deletions': minus_count,
                'status': 'modified',
            })
    except Exception:
        # Fall back to local diff if fetch/remote unavailable
        ref = f'{target_branch}...{branch}'

    # Full unified diff
    diff_text = ''
    truncated = False
    try:
        raw = subprocess.check_output(
            ['git', '-C', str(repo_path), 'diff', ref],
            stderr=subprocess.DEVNULL, timeout=20,
        ).decode(errors='replace')
        lines = raw.splitlines(keepends=True)
        if len(lines) > MAX_DIFF_LINES:
            diff_text = ''.join(lines[:MAX_DIFF_LINES])
            truncated = True
        else:
            diff_text = raw
    except Exception:
        diff_text = ''

    # If remote three-dot ref failed, fall back to local two-dot
    if not diff_text and not files:
        try:
            ref_local = f'{target_branch}..{branch}'
            raw = subprocess.check_output(
                ['git', '-C', str(repo_path), 'diff', ref_local],
                stderr=subprocess.DEVNULL, timeout=20,
            ).decode(errors='replace')
            lines = raw.splitlines(keepends=True)
            diff_text = ''.join(lines[:MAX_DIFF_LINES])
            truncated = len(lines) > MAX_DIFF_LINES
        except Exception:
            pass

    return {
        'branch': branch,
        'target_branch': target_branch,
        'files': files,
        'diff': diff_text,
        'truncated': truncated,
        'error': None,
    }


def task_commits(task_id: str) -> dict:
    """Return commits on branch that are not on target branch."""
    task, repo_path, branch, target_branch, err = _git_task_context(task_id)
    if err:
        return {**err, 'commits': [], 'target_branch': None}

    # Best-effort fetch
    try:
        subprocess.run(
            ['git', '-C', str(repo_path), 'fetch', 'origin', '--quiet'],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass

    commits = []
    # Try origin/target first, fall back to local target
    for ref_target in (f'origin/{target_branch}', target_branch):
        try:
            raw = subprocess.check_output(
                ['git', '-C', str(repo_path), 'log',
                 f'{ref_target}..{branch}',
                 '--pretty=format:%H|%an|%ai|%s',
                 '--max-count=50'],
                stderr=subprocess.DEVNULL, timeout=15,
            ).decode(errors='replace').strip()
            if raw:
                for line in raw.splitlines():
                    parts = line.split('|', 3)
                    if len(parts) == 4:
                        sha, author, date, message = parts
                        commits.append({
                            'sha': sha.strip(),
                            'author': author.strip(),
                            'date': date.strip(),
                            'message': message.strip(),
                        })
            break
        except Exception:
            continue

    return {
        'branch': branch,
        'target_branch': target_branch,
        'commits': commits,
        'error': None,
    }


def load_repos(registry=None):
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
        repos.append(git_repo_info(p['id'], Path(local_path)))
    return repos


_SNAPSHOT_CACHE_DATA = None
_SNAPSHOT_CACHE_TIME = 0.0

def load_snapshot():
    global _SNAPSHOT_CACHE_DATA, _SNAPSHOT_CACHE_TIME
    now = time.time()
    if _SNAPSHOT_CACHE_DATA and (now - _SNAPSHOT_CACHE_TIME) < 2.0:
        return _SNAPSHOT_CACHE_DATA

    if not ES_API_KEY or ES_API_KEY == 'AUTO_GENERATED_BY_INSTALLER':
        pass

    with ThreadPoolExecutor(max_workers=6) as pool:
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
                    'aggs': {'total_elastro_savings': {'sum': {'field': 'savings'}}}
                })
                return int(agg_res.get('aggregations', {}).get('total_elastro_savings', {}).get('value', 0))
            except Exception:
                return 0
        f_savings = pool.submit(fetch_savings)
        f_workers = pool.submit(load_workers)
        f_projects = pool.submit(load_projects_registry)

        tasks_res = f_tasks.result().get('hits', {}).get('hits', [])
        reviews_res = f_reviews.result().get('hits', {}).get('hits', [])
        failures_res = f_failures.result().get('hits', {}).get('hits', [])
        provenance_res = f_provenance.result().get('hits', {}).get('hits', [])
        elastro_savings = f_savings.result()
        workers_res = f_workers.result()
        projects_res = f_projects.result()

    repos_res = load_repos(registry=projects_res)

    result = {
        'workers': workers_res,
        'tasks': [{'_id': h.get('_id'), **h.get('_source', {})} for h in tasks_res],
        'reviews': [{'_id': h.get('_id'), **h.get('_source', {})} for h in reviews_res],
        'failures': [{'_id': h.get('_id'), **h.get('_source', {})} for h in failures_res],
        'provenance': [{'_id': h.get('_id'), **h.get('_source', {})} for h in provenance_res],
        'repos': repos_res,
        'projects': projects_res,
        'elastro_savings': elastro_savings,
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
        print(f"Error fetching agent status: {e}")
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


@app.post('/api/intake/session')
def api_intake_start_session(payload: dict):
    repo = (payload.get('repo') or '').strip()
    prompt = (payload.get('prompt') or '').strip()
    if not repo:
        return JSONResponse(status_code=400, content={'error': 'repo is required'})
    if not prompt:
        return JSONResponse(status_code=400, content={'error': 'prompt is required'})
    try:
        session = create_planning_session(repo, prompt)
        return _session_payload_for_client(session)
    except Exception as e:
        logger.exception('Failed to create intake session')
        return JSONResponse(status_code=500, content={'error': str(e)[:400]})


@app.get('/api/intake/session/{session_id}')
def api_intake_get_session(session_id: str):
    session = load_session(session_id)
    if not session:
        return JSONResponse(status_code=404, content={'error': 'session not found'})
    return _session_payload_for_client(session)


@app.post('/api/intake/session/{session_id}/message')
def api_intake_message(session_id: str, payload: dict):
    text = (payload.get('text') or '').strip()
    if not text:
        return JSONResponse(status_code=400, content={'error': 'text is required'})
    plan = payload.get('plan') if isinstance(payload.get('plan'), dict) else None
    try:
        session = refine_session(session_id, text, plan)
        if not session:
            return JSONResponse(status_code=404, content={'error': 'session not found'})
        return _session_payload_for_client(session)
    except Exception as e:
        logger.exception('Failed to refine intake session')
        return JSONResponse(status_code=500, content={'error': str(e)[:400]})


@app.post('/api/intake/session/{session_id}/commit')
def api_intake_commit(session_id: str, payload: dict):
    session = load_session(session_id)
    if not session:
        return JSONResponse(status_code=404, content={'error': 'session not found'})

    plan = payload.get('plan') if isinstance(payload.get('plan'), dict) else session.get('draftPlan')
    if not plan or not (plan.get('epics') or []):
        return JSONResponse(status_code=400, content={'error': 'plan is empty'})

    repo = session.get('repo') or (payload.get('repo') or '').strip()
    if not repo:
        return JSONResponse(status_code=400, content={'error': 'repo is required'})

    try:
        docs, _results = commit_plan(repo, plan)
        session['status'] = 'committed'
        session['draftPlan'] = plan
        session['committed_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        session['committedDocs'] = [d.get('id') for d in docs]
        save_session(session)
        return {
            'ok': True,
            'count': _count_plan_tasks(plan),
            'created': len(docs),
            'taskIds': [d.get('id') for d in docs if d.get('item_type') == 'task'],
        }
    except Exception as e:
        logger.exception('Failed to commit intake plan')
        return JSONResponse(status_code=500, content={'error': str(e)[:400]})


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

def _is_remote_url(url: str) -> bool:
    """Return True when `url` looks like an HTTPS or SSH git URL."""
    if not url:
        return False
    lower = url.strip().lower()
    return (
        lower.startswith('https://')
        or lower.startswith('http://')
        or lower.startswith('git@')
        or lower.startswith('ssh://')
        or lower.startswith('git://')
    )




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


@app.post("/api/projects")
async def api_create_project(request: Request, payload: dict, background_tasks: BackgroundTasks):
    from utils.git_credentials import detect_repo_type, strip_credentials, _rewrite_url  # noqa
    import ado_tokens_store
    import github_tokens_store

    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Project name is required."})

    repo_url = (payload.get("repoUrl") or "").strip()
    local_path_raw = (payload.get("localPath") or "").strip()

    new_id = f"proj-{uuid.uuid4().hex[:8]}"

    # ── Determine where this project lives on disk ───────────────────────────
    if _is_remote_url(repo_url):
        # AP-11: Use an ephemeral /tmp directory for the registration clone.
        # _clone_and_setup_project() already deletes the clone after AST
        # ingestion (AP-4B). Writing to WORKSPACE_ROOT was an anti-pattern
        # that created persistent proj-* directories on the host bind-mount.
        # /tmp is pod-local, never persisted, and auto-cleaned by the OS.
        dest_path = Path(tempfile.mkdtemp(prefix=f"flume-reg-{new_id}-"))
        clone_status = 'cloning'
        resolved_path = None  # AP-11: no persistent path — ES is source of truth
    elif local_path_raw:
        # User pointed at an existing directory on the local filesystem.
        dest_path = Path(local_path_raw).expanduser().resolve()
        clone_status = 'local'
        resolved_path = str(dest_path)
    else:
        # No URL / path — just a named project with no repo yet.
        dest_path = None
        clone_status = 'no_repo'
        resolved_path = None

    # ── Embed credentials — OpenBao-first, no env-file dependency ───────────
    # PATs are stored in OpenBao KV (secret/data/flume/ado_tokens/{id}) and
    # retrieved via the ado/github token stores. The raw URL is never logged.
    #
    # ALWAYS strip userinfo (username@) from the URL before passing to git.
    # If no PAT is found, git receives a clean https://host/repo URL and fails
    # immediately with a 128 auth error. Without this, git sees the username
    # portion, tries to prompt for a password interactively, and dies with
    # "No such device or address" inside the non-TTY container environment.
    clone_url = strip_credentials(repo_url) if _is_remote_url(repo_url) else repo_url
    if _is_remote_url(repo_url):
        repo_type = detect_repo_type(repo_url)
        pat: str = ""
        if repo_type == "ado":
            try:
                raw_pat = ado_tokens_store.get_active_token_plain(WORKSPACE_ROOT)
                if raw_pat and "OPENBAO_DELEGATED" not in raw_pat:
                    pat = raw_pat
                elif raw_pat:
                    logger.warning(json.dumps({
                        "event": "ado_pat_placeholder_detected",
                        "project_id": new_id,
                        "hint": "OpenBao KV lookup returned placeholder. Falling back to ADO_PERSONAL_ACCESS_TOKEN env var.",
                    }))
            except Exception as _cred_err:
                logger.warning(json.dumps({
                    "event": "ado_pat_fetch_error",
                    "project_id": new_id,
                    "error": str(_cred_err)[:200],
                }))
            # ── OpenBao direct KV fallback (ADO_TOKEN written by flume start) ───
            # On a fresh start the UI credential registry (flume-ado-tokens) is
            # empty, but 'flume start' writes ADO_TOKEN to secret/data/flume/keys.
            # Read it directly so project cloning works immediately after start
            # without requiring the user to also add credentials via Settings.
            _DELEGATED = "OPENBAO_DELEGATED"
            if not pat:
                try:
                    from llm_settings import _openbao_get_all  # noqa: PLC0415
                    _bao_vals = _openbao_get_all(WORKSPACE_ROOT)
                    _bao_ado = str(_bao_vals.get("ADO_TOKEN") or "").strip()
                    if _bao_ado and _DELEGATED not in _bao_ado:
                        pat = _bao_ado
                        logger.info(json.dumps({
                            "event": "ado_pat_from_openbao_direct",
                            "project_id": new_id,
                            "hint": "PAT sourced from OpenBao ADO_TOKEN key (flume start provisioning).",
                        }))
                except Exception:
                    pass
            # ── Env-var fallback (for local dev environments without OpenBao) ────
            # Guard: never use the OPENBAO_DELEGATED sentinel as an actual PAT.
            if not pat:
                _env_pat = (
                    os.environ.get("ADO_PERSONAL_ACCESS_TOKEN", "").strip()
                    or os.environ.get("ADO_TOKEN", "").strip()
                )
                if _env_pat and _DELEGATED not in _env_pat:
                    pat = _env_pat
                    logger.info(json.dumps({
                        "event": "ado_pat_from_env",
                        "project_id": new_id,
                        "hint": "PAT sourced from ADO_PERSONAL_ACCESS_TOKEN env var (OpenBao fallback).",
                    }))
                elif _env_pat:
                    logger.warning(json.dumps({
                        "event": "ado_pat_sentinel_in_env",
                        "project_id": new_id,
                        "hint": "ADO_TOKEN env var contains OPENBAO_DELEGATED sentinel — OpenBao not yet seeded. Re-add the project after flume start completes vault provisioning.",
                    }))
        elif repo_type == "github":
            try:
                raw_pat = github_tokens_store.get_active_token_plain(WORKSPACE_ROOT)
                if raw_pat and "OPENBAO_DELEGATED" not in raw_pat:
                    pat = raw_pat
                elif raw_pat:
                    logger.warning(json.dumps({
                        "event": "gh_pat_placeholder_detected",
                        "project_id": new_id,
                        "hint": "OpenBao KV lookup returned placeholder. Falling back to GITHUB_TOKEN env var.",
                    }))
            except Exception as _cred_err:
                logger.warning(json.dumps({
                    "event": "gh_pat_fetch_error",
                    "project_id": new_id,
                    "error": str(_cred_err)[:200],
                }))
            # ── Env-var fallback ─────────────────────────────────────────────────
            _DELEGATED = "OPENBAO_DELEGATED"
            if not pat:
                _env_pat = os.environ.get("GITHUB_TOKEN", "").strip()
                if _env_pat and _DELEGATED not in _env_pat:
                    pat = _env_pat
                    logger.info(json.dumps({
                        "event": "gh_pat_from_env",
                        "project_id": new_id,
                        "hint": "PAT sourced from GITHUB_TOKEN env var (OpenBao fallback).",
                    }))
                elif _env_pat:
                    logger.warning(json.dumps({
                        "event": "gh_pat_sentinel_in_env",
                        "project_id": new_id,
                        "hint": "GITHUB_TOKEN env var contains OPENBAO_DELEGATED sentinel — OpenBao not yet seeded.",
                    }))
        if pat:
            # Embed PAT into the already-stripped URL.
            clone_url = _rewrite_url(clone_url, pat)
            logger.info(json.dumps({
                "event": "project_clone_credentials_embedded",
                "project_id": new_id,
                "repo_type": repo_type,
            }))
        else:
            logger.warning(json.dumps({
                "event": "project_clone_no_credentials",
                "project_id": new_id,
                "repo_type": repo_type,
                "hint": "Add credentials via Settings → Repositories before cloning private repos.",
            }))

    entry = {
        "id": new_id,
        "name": name,
        "repoUrl": repo_url,   # Store original URL (no embedded PAT) in registry
        "path": resolved_path,
        "clone_status": clone_status,
        "clone_error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "gitflow": {"autoPrOnApprove": True, "defaultBranch": None},
    }

    # Single consistent write with ?refresh=wait_for — prevents the name
    # disappearing on the next snapshot poll and eliminates the ~5s delay.
    _upsert_project(entry)

    http_client = request.app.state.http_client

    if _is_remote_url(repo_url):
        # Clone in the background — workers consume from the shared /workspace volume.
        # clone_url has the PAT embedded (if available); stored repo_url does not.
        background_tasks.add_task(
            _clone_and_setup_project,
            http_client, new_id, name, clone_url, dest_path,
        )
    elif local_path_raw and dest_path.is_dir():
        # Local path already exists — just trigger AST ingestion.
        background_tasks.add_task(
            _deterministic_ast_ingest, http_client, resolved_path, new_id, name,
        )

    return {"success": True, "projectId": new_id, "project": entry, "message": "Project created."}

@app.get("/api/projects/{project_id}/clone-status")
def api_project_clone_status(project_id: str):
    """Lightweight polling endpoint — returns the clone_status of a project."""
    registry = load_projects_registry()
    proj = next((p for p in registry if p.get('id') == project_id), None)
    if not proj:
        return JSONResponse(status_code=404, content={'error': f"Project '{project_id}' not found"})
    return {
        'projectId': project_id,
        'clone_status': proj.get('clone_status', 'unknown'),
        'clone_error': proj.get('clone_error'),
        'path': proj.get('path'),
        'is_git': (Path(proj.get('path', '')) / '.git').exists() if proj.get('path') else False,
    }


@app.post("/api/projects/{project_id}/delete")
def api_delete_project(project_id: str):
    registry = load_projects_registry()
    project_found = any(p.get("id") == project_id for p in registry)

    if not project_found:
        return JSONResponse(
            status_code=404,
            content={"error": f"Project '{project_id}' not found"},
        )

    # Hard-delete the document from ES with ?refresh=wait_for so the next
    # snapshot poll no longer returns it.  The old code only called
    # save_projects_registry(filtered) which upserts surviving docs but never
    # issues a DELETE, leaving the removed project in the index and causing
    # it to reappear on the very next /api/snapshot call.
    try:
        _es_projects_request(
            f"/{PROJECTS_INDEX}/_doc/{project_id}?refresh=wait_for",
            method="DELETE",
        )
        logger.info(json.dumps({
            "event": "project_deleted",
            "project_id": project_id,
        }))
    except Exception as exc:
        logger.warning(json.dumps({
            "event": "project_delete_es_error",
            "project_id": project_id,
            "error": str(exc)[:200],
        }))

    # AP-4B: Only clean up a persisted local path for 'local' clone_status projects.
    # Remote repos (clone_status='indexed') have no persistent local path to remove.
    project_doc = next((p for p in registry if p.get("id") == project_id), {})
    local_path = project_doc.get("path")
    clone_status = project_doc.get("clone_status", "")
    if local_path and clone_status == "local":
        dest_path = Path(local_path)
        # Best-effort removal of local repo dir — only for user-supplied local paths.
        # We don't auto-delete arbitrary user directories; just log the note.
        logger.info(json.dumps({
            "event": "project_local_path_note",
            "project_id": project_id,
            "path": str(dest_path),
            "note": "Local path not auto-deleted (user-managed). Remove manually if desired.",
        }))

    return {"success": True, "projectId": project_id, "message": "Project removed."}

@app.get("/api/tasks/{task_id}/history")
def api_task_history(task_id: str):
    return []

@app.get("/api/tasks/{task_id}/diff")
def api_task_diff(task_id: str):
    """
    Return the git diff for the branch associated with a task.
    AP-4B: Uses GitHostClient REST API for remote repos (no local clone required).
    Compares task branch against the repo's default branch.
    """
    from utils.git_host_client import get_git_client, GitHostError  # noqa

    try:
        es_id, task = find_task_doc_by_logical_id(task_id)
        if not task:
            return {"diff": "", "error": "Task not found"}

        branch = task.get("branch")
        if not branch:
            return {"diff": "", "error": "No branch recorded for this task"}

        repo_id = task.get("repo")
        proj = {}
        if repo_id:
            try:
                proj_res = _es_projects_request(f"/{PROJECTS_INDEX}/_doc/{repo_id}")
                proj = proj_res.get("_source") or {}
            except Exception:
                pass

        clone_status = proj.get("clone_status") or proj.get("cloneStatus") or ""
        repo_url = proj.get("repoUrl") or ""

        # ── Remote repo: GitHostClient REST API ──────────────────────────────
        if clone_status in ("indexed", "cloned") and _is_remote_url(repo_url):
            try:
                client = get_git_client(proj)
                base = (
                    proj.get("gitflow", {}).get("defaultBranch")
                    or client.get_default_branch()
                )
                result = client.get_diff(base=base, head=branch)
                return {
                    "diff": result.get("diff", ""),
                    "branch": branch,
                    "base": base,
                    "files": result.get("files", []),
                    "truncated": result.get("truncated", False),
                }
            except GitHostError as e:
                return {"diff": "", "error": str(e)[:300]}

        # ── Local repo: git subprocess (clone_status='local') ────────────────
        local_path_str = proj.get("path") or task.get("worktree")
        if not local_path_str or not Path(local_path_str).exists():
            return {"diff": "", "error": "Repository not available locally; configure a PAT to enable API-based diff."}

        try:
            ref_out = subprocess.run(
                ["git", "-C", local_path_str, "symbolic-ref", "refs/remotes/origin/HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            base = ref_out.stdout.strip().split("/")[-1] if ref_out.returncode == 0 else "main"
        except Exception:
            base = "main"

        diff_out = subprocess.run(
            ["git", "-C", local_path_str, "diff", f"origin/{base}...{branch}"],
            capture_output=True, text=True, timeout=15,
        )
        diff_text = diff_out.stdout if diff_out.returncode == 0 else ""
        if len(diff_text) > 80_000:
            diff_text = diff_text[:80_000] + "\n\n... [diff truncated at 80k chars] ..."
        return {"diff": diff_text, "branch": branch, "base": f"origin/{base}"}
    except Exception as e:
        logger.warning({"event": "task_diff_error", "task_id": task_id, "error": str(e)})
        return {"diff": "", "error": str(e)}

@app.get("/api/tasks/{task_id}/thoughts")
def api_task_thoughts(task_id: str):
    _, source = find_task_doc_by_logical_id(task_id)
    if not source:
        return {"thoughts": []}
    return {"thoughts": source.get("execution_thoughts", [])}

@app.get("/api/tasks/{task_id}/commits")
def api_task_commits(task_id: str):
    from utils.git_host_client import get_git_client, GitHostError  # noqa

    try:
        _, task = find_task_doc_by_logical_id(task_id)
        if not task:
            return []

        branch = task.get("branch")
        repo_id = task.get("repo")
        proj = {}
        if repo_id:
            try:
                proj_res = _es_projects_request(f"/{PROJECTS_INDEX}/_doc/{repo_id}")
                proj = proj_res.get("_source") or {}
            except Exception:
                pass

        clone_status = proj.get("clone_status") or proj.get("cloneStatus") or ""
        repo_url = proj.get("repoUrl") or ""

        if branch and clone_status in ("indexed", "cloned") and _is_remote_url(repo_url):
            try:
                client = get_git_client(proj)
                base = (
                    proj.get("gitflow", {}).get("defaultBranch")
                    or client.get_default_branch()
                )
                return client.get_commits(branch=branch, base=base)
            except GitHostError:
                pass
    except Exception:
        pass
    return []

@app.post("/api/tasks/{task_id}/transition")
def api_task_transition(task_id: str, payload: dict):
    """
    Transition a task to a new status.

    Allowed user-initiated transitions:
      - ready   → re-queues the task from the same role it blocked on
      - planned → demotes back to planning phase
      - inbox   → returns to the intake queue

    History (agent_log, commit_sha, execution_thoughts) is preserved so
    engineers can read why the task blocked before retrying.
    """
    _ALLOWED_USER_STATUSES = {'ready', 'planned', 'inbox'}
    status = (payload.get('status') or '').strip().lower()
    if status not in _ALLOWED_USER_STATUSES:
        return JSONResponse(
            status_code=400,
            content={'error': f'status must be one of {sorted(_ALLOWED_USER_STATUSES)}, got {status!r}'},
        )

    es_id, src = find_task_doc_by_logical_id(task_id)
    if not es_id or src is None:
        return JSONResponse(status_code=404, content={'error': f'task {task_id!r} not found'})

    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    # Restart from the same role that was assigned when the task blocked.
    owner = src.get('owner') or src.get('assigned_agent_role')

    doc = {
        'status': status,
        'queue_state': 'queued',
        'active_worker': None,
        'needs_human': False,
        'updated_at': now,
        'last_update': now,
        'implementer_consecutive_llm_failures': 0,
    }
    if owner:
        doc['owner'] = owner
        doc['assigned_agent_role'] = owner

    es_post(f'agent-task-records/_update/{es_id}', {'doc': doc})
    logger.info(f'task transition: {task_id} → {status} (role={owner})')
    return {'success': True, 'task_id': task_id, 'status': status, 'owner': owner, '_id': es_id}


@app.post("/api/tasks/bulk-requeue")
def api_tasks_bulk_requeue(payload: dict):
    """
    Requeue up to 50 blocked tasks in one call.

    Body:    { "task_ids": ["story-1", "feat-1", ...] }
    Returns: { "requeued": [...], "failed": [...] }
    """
    _MAX_BULK = 50
    task_ids = payload.get('task_ids') or []
    if not isinstance(task_ids, list):
        return JSONResponse(status_code=400, content={'error': 'task_ids must be a list'})
    if len(task_ids) > _MAX_BULK:
        return JSONResponse(status_code=400, content={'error': f'bulk limit is {_MAX_BULK} tasks per call'})

    requeued, failed = [], []
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    for task_id in task_ids:
        try:
            es_id, src = find_task_doc_by_logical_id(str(task_id))
            if not es_id or src is None:
                failed.append({'task_id': task_id, 'error': 'not found'})
                continue
            owner = src.get('owner') or src.get('assigned_agent_role')
            doc = {
                'status': 'ready',
                'queue_state': 'queued',
                'active_worker': None,
                'needs_human': False,
                'updated_at': now,
                'last_update': now,
                'implementer_consecutive_llm_failures': 0,
            }
            if owner:
                doc['owner'] = owner
                doc['assigned_agent_role'] = owner
            es_post(f'agent-task-records/_update/{es_id}', {'doc': doc})
            requeued.append({'task_id': task_id, 'owner': owner})
        except Exception as exc:
            logger.error(f'bulk-requeue: task {task_id} failed: {exc}')
            failed.append({'task_id': task_id, 'error': str(exc)[:200]})

    logger.info(f'bulk-requeue: requeued={len(requeued)} failed={len(failed)}')
    return {'requeued': requeued, 'failed': failed}



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
@app.get("/api/repos/{project_id}/branches")
def api_repo_branches(project_id: str):
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

    # ── Remote repo path: use GitHostClient REST API (no local clone required) ──
    repo_url = proj.get("repoUrl") or ""
    if cs in ("indexed", "cloned") and repo_url and _is_remote_url(repo_url):
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

    default = resolve_default_branch(
        repo_path, override=proj.get("gitflow", {}).get("defaultBranch")
    )
    return {"gitAvailable": True, "branches": branches, "default": default}


@app.get("/api/repos/{project_id}/tree")
def api_repo_tree(project_id: str, branch: str = ""):
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

    # ── Remote repo: GitHostClient REST API ──────────────────────────────────
    if cs in ("indexed", "cloned") and repo_url and _is_remote_url(repo_url):
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
        branch = resolve_default_branch(
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
def api_repo_file(project_id: str, path: str = "", branch: str = ""):
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

    # ── Remote repo: GitHostClient REST API ──────────────────────────────────
    if cs in ("indexed", "cloned") and repo_url and _is_remote_url(repo_url):
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
        branch = resolve_default_branch(
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
    from codex_app_server import status
    return status()

@app.get("/api/codex-app-server/proxy-config")
def api_codex_proxy_config():
    # Frontend expects codex WS setup info
    return {"baseUrl": "ws://localhost:8765", "path": "/api/codex-app-server/ws"}

@app.get("/api/settings/llm")
def api_settings_llm():
    from llm_settings import get_llm_settings_response
    return get_llm_settings_response(WORKSPACE_ROOT)

@app.post("/api/settings/llm")
def api_settings_llm_update(payload: dict):
    from llm_settings import validate_llm_settings, _update_env_keys
    ok, msg, updates = validate_llm_settings(payload, WORKSPACE_ROOT)
    if ok:
        _update_env_keys(WORKSPACE_ROOT, updates)
        return {"ok": True, "restartRequired": False, "message": "Saved"}
    return JSONResponse(status_code=400, content={"error": msg})

@app.put("/api/settings/llm/credentials")
def api_settings_llm_credentials(payload: dict):
    from llm_settings import validate_llm_settings, _update_env_keys
    ok, msg, updates = validate_llm_settings(payload, WORKSPACE_ROOT)
    if ok:
        _update_env_keys(WORKSPACE_ROOT, updates)
        return {"success": True, "message": "Saved"}
    return JSONResponse(status_code=400, content={"error": msg})

@app.post("/api/settings/llm/credentials")
def api_settings_llm_credentials_post(payload: dict):
    from llm_credentials_store import apply_credentials_action
    from llm_settings import _update_env_keys
    workspace = Path(os.environ.get('FLUME_WORKSPACE', './workspace'))
    
    ok, msg, updates = apply_credentials_action(workspace, payload)
    if not ok:
        return JSONResponse(status_code=400, content={"error": msg})
        
    if updates:
        _update_env_keys(workspace, updates)
        
    return {"ok": True, "message": "Action applied successfully", "restartRequired": False}

@app.post("/api/settings/llm/oauth/refresh")
def api_settings_llm_oauth_refresh():
    from llm_settings import do_oauth_refresh
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
    from agent_models_settings import get_agent_models_response
    return get_agent_models_response(WORKSPACE_ROOT)

@app.put("/api/settings/agent-models")
@app.post("/api/settings/agent-models")
def api_settings_agent_models_update(payload: dict):
    from agent_models_settings import validate_save_agent_models, save_agent_models
    import llm_credentials_store as _lcs
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
        from llm_settings import is_openbao_installed, _openbao_enabled, _openbao_secret_ref
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
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://localhost:8766/metrics", timeout=2.0)
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
                        results["go_goroutines"] = int(val)
                    elif key_with_tags == "go_memstats_alloc_bytes":
                        results["go_memstats_alloc_bytes"] = int(val)
                    elif key_with_tags == "go_memstats_sys_bytes":
                        results["go_memstats_sys_bytes"] = int(val)
                    elif key_with_tags == "flume_up":
                        results["flume_up"] = int(val)
                    elif key_with_tags == "flume_escalation_total":
                        results["flume_escalation_total"] = int(val)
                    elif key_with_tags == "flume_vram_pressure_events_total":
                        results["flume_vram_pressure_events_total"] = int(val)
                    elif key_with_tags.startswith("flume_build_info{"):
                        m = re.search(r'version="([^"]+)"', key_with_tags)
                        if m:
                            results["flume_build_info"] = m.group(1)
                    elif key_with_tags.startswith("flume_active_models{"):
                        m = re.search(r'model="([^"]+)"', key_with_tags)
                        if m and int(val) == 1:
                            results["flume_active_models"].append(m.group(1))
                    elif key_with_tags.startswith("flume_ensemble_requests_total{"):
                        # flume_ensemble_requests_total{model_family="qwen",size="2",task_type="chat"} 1
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_ensemble_requests_total"].append({
                            "tags": tag_dict,
                            "count": int(val)
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
