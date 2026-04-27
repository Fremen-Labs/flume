"""Settings API router — LLM, Repos, Agent Models, System settings.

Extracted from server.py as part of the modular router decomposition.
"""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api.models import (
    LogLevelRequest,
    ClientLogRequest,
    LLMSettingsRequest,
    LLMCredentialsActionRequest,
    RepoSettingsRequest,
    AgentModelsRequest,
    SystemSettingsRequest,
)
from utils.logger import get_logger, set_global_log_level
from utils.workspace import resolve_safe_workspace
from core.elasticsearch import async_es_search, async_es_post

import httpx

logger = get_logger(__name__)
WORKSPACE_ROOT = resolve_safe_workspace()
_SRC_ROOT = Path(__file__).resolve().parent.parent.parent  # src/

router = APIRouter()


# ── Log Level ──────────────────────────────────────────────────────────────────

@router.post('/api/settings/log-level')
async def api_set_log_level(payload: LogLevelRequest):
    level = payload.level.upper()
    set_global_log_level(level)

    # Proxy to Gateway
    try:
        from config import get_settings
        gw_url = get_settings().GATEWAY_URL.rstrip('/')
        async with httpx.AsyncClient() as client:
            await client.post(f"{gw_url}/internal/level", json={"level": level}, timeout=2.0)
    except httpx.RequestError as e:
        logger.warning({"event": "log_level_gateway_sync_failed", "error": str(e)[:200]})

    return {"ok": True, "level": level}


@router.post('/api/logs/client')
async def api_client_logs(payload: ClientLogRequest):
    log_level = (payload.level or "ERROR").upper()
    log_func = {
        "DEBUG": logger.debug,
        "INFO": logger.info,
        "WARNING": logger.warning,
        "WARN": logger.warning,
        "ERROR": logger.error,
    }.get(log_level, logger.error)
    log_func(
        payload.message,
        extra={"structured_data": {"source": "browser", **(payload.data or {})}}
    )
    return {"ok": True}


# ── LLM Settings ──────────────────────────────────────────────────────────────

@router.get("/api/settings/llm")
def api_settings_llm():
    from llm_settings import get_llm_settings_response  # type: ignore
    return get_llm_settings_response(WORKSPACE_ROOT)


@router.post("/api/settings/llm")
def api_settings_llm_update(payload: LLMSettingsRequest):
    from llm_settings import validate_llm_settings, _update_env_keys  # type: ignore
    ok, msg, updates = validate_llm_settings(payload.model_dump(exclude_none=False), WORKSPACE_ROOT)
    if ok:
        _update_env_keys(WORKSPACE_ROOT, updates)
        return {"ok": True, "restartRequired": False, "message": "Saved"}
    return JSONResponse(status_code=400, content={"error": msg})


@router.put("/api/settings/llm/credentials")
def api_settings_llm_credentials(payload: LLMSettingsRequest):
    from llm_settings import validate_llm_settings, _update_env_keys  # type: ignore
    ok, msg, updates = validate_llm_settings(payload.model_dump(exclude_none=False), WORKSPACE_ROOT)
    if ok:
        _update_env_keys(WORKSPACE_ROOT, updates)
        return {"success": True, "message": "Saved"}
    return JSONResponse(status_code=400, content={"error": msg})


@router.post("/api/settings/llm/credentials")
def api_settings_llm_credentials_post(payload: LLMCredentialsActionRequest):
    from llm_credentials_store import apply_credentials_action  # type: ignore
    from llm_settings import _update_env_keys
    from config import get_settings
    workspace = Path(get_settings().FLUME_WORKSPACE)

    ok, msg, updates = apply_credentials_action(workspace, payload.model_dump(exclude_none=False))
    if not ok:
        return JSONResponse(status_code=400, content={"error": msg})

    if updates:
        _update_env_keys(workspace, updates)

    return {"ok": True, "message": "Action applied successfully", "restartRequired": False, "credential_id": msg if msg else ""}


@router.post("/api/settings/llm/oauth/refresh")
def api_settings_llm_oauth_refresh():
    from llm_settings import do_oauth_refresh  # type: ignore
    ok, msg, token = do_oauth_refresh(WORKSPACE_ROOT)
    if ok:
        return {"success": True, "message": msg, "token": token}
    return JSONResponse(status_code=400, content={"error": msg})


# ── Repo Settings ──────────────────────────────────────────────────────────────

@router.get("/api/settings/repos")
def api_settings_repos():
    from repo_settings import get_repo_settings_response  # noqa: PLC0415
    return get_repo_settings_response(WORKSPACE_ROOT)


@router.put("/api/settings/repos")
def api_settings_repos_update(payload: RepoSettingsRequest):
    from repo_settings import update_repo_settings  # noqa: PLC0415
    ok, msg = update_repo_settings(WORKSPACE_ROOT, payload.model_dump(exclude_none=False))
    if ok:
        return {"success": True, "message": msg}
    return JSONResponse(status_code=400, content={"error": msg})


# ── System Settings ────────────────────────────────────────────────────────────

@router.get("/api/settings/system")
async def get_system_settings():
    import httpx
    sys_conf = {}
    try:
        doc = await async_es_search('flume-settings', {'query': {'term': {'_id': 'system'}}})
        if doc and 'hits' in doc and doc['hits']['hits']:
            sys_conf = doc['hits']['hits'][0]['_source']
    except (httpx.RequestError, httpx.HTTPStatusError, KeyError, ValueError, TypeError):
        logger.debug("get_system_settings: ES read failed, using env defaults", exc_info=True)

    from config import get_settings
    _s = get_settings()
    return {
        "es_url": _s.ES_URL or sys_conf.get('es_url', 'http://127.0.0.1:9200'),
        "es_api_key": "***" if _s.ES_API_KEY or sys_conf.get('es_api_key') else "",
        "es_verify_tls": _s.ES_VERIFY_TLS or sys_conf.get('es_verify_tls', False),
        "openbao_url": _s.OPENBAO_URL or sys_conf.get('openbao_url', 'http://127.0.0.1:8200'),
        "vault_token": "••••" if _s.VAULT_TOKEN or sys_conf.get('vault_token') else "",
        "prometheus_enabled": sys_conf.get('prometheus_enabled', True)
    }


@router.put("/api/settings/system")
async def update_system_settings(settings: SystemSettingsRequest):
    import httpx
    try:
        doc = await async_es_search('flume-settings', {'query': {'term': {'_id': 'system'}}})
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
        if hasattr(settings, 'es_verify_tls') and settings.es_verify_tls is not None:
            sys_conf['es_verify_tls'] = settings.es_verify_tls

        await async_es_post('flume-settings/_doc/system', sys_conf)
        return {"status": "ok"}
    except (httpx.RequestError, httpx.HTTPStatusError, KeyError, ValueError, TypeError) as e:
        logger.error({"event": "update_system_settings_failed", "error": str(e)[:300]}, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)[:400]})


# ── Agent Models ───────────────────────────────────────────────────────────────

@router.get("/api/settings/agent-models")
def api_settings_agent_models():
    from agent_models_settings import get_agent_models_response  # type: ignore
    return get_agent_models_response(WORKSPACE_ROOT)


@router.put("/api/settings/agent-models")
@router.post("/api/settings/agent-models")
def api_settings_agent_models_update(payload: AgentModelsRequest):
    from agent_models_settings import validate_save_agent_models, save_agent_models  # type: ignore
    import llm_credentials_store as _lcs  # type: ignore
    # Map useGlobal flag from the new frontend to Settings default credential.
    roles = payload.roles or {}
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


# ── Restart ────────────────────────────────────────────────────────────────────

@router.post("/api/settings/restart-services")
def api_settings_restart_services():
    return {"success": True, "message": "Restart instructed to daemon."}
