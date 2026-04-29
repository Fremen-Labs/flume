#!/usr/bin/env python3
# ruff: noqa: E402
"""Flume Server — Central intelligence and frontend orchestration."""
from pathlib import Path
import json
import os
import re
import sys
import threading
import time
import httpx

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from contextlib import asynccontextmanager
from datetime import datetime, timezone

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
from utils.exceptions import SAFE_EXCEPTIONS


async def _async_verify_es_index():
    """Non-blocking ES index verification during ASGI startup."""
    from utils.logger import get_logger as _get_startup_logger
    _startup_logger = _get_startup_logger('es_bootstrap')
    try:
        from config import get_settings
        _s = get_settings()
        _es_check_url = _s.ES_URL or ('http://elasticsearch:9200' if _s.FLUME_NATIVE_MODE != '1' else 'http://localhost:9200')
        from core.elasticsearch import _get_auth_headers as _boot_auth, _get_httpx_verify
        async with httpx.AsyncClient(verify=_get_httpx_verify()) as client:
            resp = await client.head(f"{_es_check_url}/agent-task-records", headers=_boot_auth(), timeout=3.0)
            if resp.status_code == 200:
                _startup_logger.info("ES index verification passed — agent-task-records exists")
            elif resp.status_code == 404:
                _startup_logger.warning("ES index 'agent-task-records' not found — was `flume start` used to boot?")
    except SAFE_EXCEPTIONS as _e:
        _startup_logger.warning(f"ES index verification skipped — cannot reach Elasticsearch: {_e}")


async def _async_seed_llm_config_from_env() -> None:
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
    from config import get_settings
    _s = get_settings()
    model = _s.LLM_MODEL.strip()
    provider = _s.LLM_PROVIDER.strip()
    base_url = _s.LLM_BASE_URL.strip()

    if not model and not provider:
        logger.debug('_async_seed_llm_config_from_env: no LLM_MODEL or LLM_PROVIDER in env — skipping')
        return

    # Build upsert payload only from non-empty env values
    doc: dict = {}
    if model:
        doc['LLM_MODEL'] = model
    if provider:
        doc['LLM_PROVIDER'] = provider
    if base_url:
        doc['LLM_BASE_URL'] = base_url

    try:
        from core.elasticsearch import async_es_search, async_es_post
        try:
            # Fetch first to check if values already set — never clobber user changes
            res = await async_es_search('flume-llm-config', {'query': {'term': {'_id': 'singleton'}}})
            hits = res.get('hits', {}).get('hits', [])
            if hits:
                existing_src = hits[0].get('_source', {})
                for k in list(doc.keys()):
                    if existing_src.get(k):
                        doc.pop(k, None)
        except SAFE_EXCEPTIONS as e:
            logger.warning(f'_async_seed_llm_config_from_env: GET error ({e}) — proceeding with full upsert')

        if not doc:
            logger.info('_async_seed_llm_config_from_env: all LLM fields already present in ES — nothing to seed')
            return

        body = {'doc': doc, 'doc_as_upsert': True}
        await async_es_post('flume-llm-config/_update/singleton', body)
        logger.info(f'_async_seed_llm_config_from_env: seeded {list(doc.keys())} → flume-llm-config (model={model})')
    except SAFE_EXCEPTIONS as e:
        logger.warning(f'_async_seed_llm_config_from_env: non-fatal failure — {e}')

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
    close_async_client,
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
from core.process_manager import maybe_auto_start_workers








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

    # Phase 1: Native async Elasticsearch bootstrap verification
    await _async_verify_es_index()

    # AP-3 resolved: _migrate_legacy_projects_json() removed — migration is complete.

    # Fallback LLM config seed: if the CLI's SeedLLMConfig write failed (e.g. due to
    # macOS Docker Desktop port-forward lag), seed from the container's env vars.
    # Uses doc_as_upsert so we never overwrite a value the user saved via the Settings UI.
    await _async_seed_llm_config_from_env()

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
    # Close the persistent httpx.AsyncClient pool used by core/elasticsearch.py
    await close_async_client()
    from core.process_manager import agents_stop
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
