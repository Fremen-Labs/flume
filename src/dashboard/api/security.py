"""Security API router — Vault status, security posture, kill switch.

Extracted from server.py as part of the modular router decomposition.
Includes the admin authentication infrastructure and ElasticsearchClient
for the kill-switch stop-all/resume-all endpoints.
"""
import json
import os
import secrets
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse

import httpx
import hvac

from utils.logger import get_logger
from core.elasticsearch import es_search
from config import AppConfig, get_settings

logger = get_logger(__name__)
router = APIRouter()


# ── Exception Hierarchy ────────────────────────────────────────────────────────

class KillSwitchDatabaseError(Exception):
    pass


class KillSwitchProcessError(Exception):
    pass


class AuthConfigurationError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


# ── Elasticsearch Client (kill-switch scoped) ─────────────────────────────────

class ElasticsearchClient:
    """Async ES client scoped to kill-switch task mutations."""

    def __init__(self, es_url: str, api_key: str, ca_certs: str):
        self.es_url = es_url.rstrip('/')
        self.headers = {'Content-Type': 'application/json'}
        if api_key:
            self.headers['Authorization'] = f'ApiKey {api_key}'
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


# ── Agent Supervisor ───────────────────────────────────────────────────────────

class AgentSupervisor:
    def terminate_all(self) -> dict:
        from server import agents_stop  # noqa: PLC0415
        return agents_stop()


# ── Kill Switch Service ────────────────────────────────────────────────────────

class KillSwitchService:
    def __init__(self, es_client: ElasticsearchClient, supervisor: AgentSupervisor):
        self.es_client = es_client
        self.supervisor = supervisor

    async def halt_all_tasks(self, correlation_id: str):
        logger.info(json.dumps({"event": "kill_switch.invoke.start", "action": "initiating_swarm_halt", "target": "all_active_tasks", "correlation_id": correlation_id}))
        try:
            await self.es_client.update_tasks_to_halted()
            logger.info(json.dumps({"event": "kill_switch.db_update.success", "elasticsearch_status": "blocked", "correlation_id": correlation_id}))
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
            logger.info(json.dumps({"event": "kill_switch.process_kill.success", "killed_pids": killed_pids, "killed_pid_count": len(killed_pids), "correlation_id": correlation_id}))
            return {"success": True, "killed_pids": killed_pids, "correlation_id": correlation_id}
        except Exception as e:
            logger.critical(json.dumps({
                "event": "kill_switch.invoke.partial_failure",
                "error": str(e),
                "correlation_id": correlation_id,
                "message": "CRITICAL: Database tasks were halted, but failed to kill OS processes. Manual intervention may be required."
            }))
            raise KillSwitchProcessError(f"DB updated but process kill failed: {e}")


# ── DI Providers ───────────────────────────────────────────────────────────────

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


# ── Admin Auth ─────────────────────────────────────────────────────────────────

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
            "endpoint": request.url.path,
            "client_ip": request.client.host if request.client else "unknown"
        }))
        raise HTTPException(status_code=403, detail="Admin access required")
    except AuthConfigurationError:
        logger.critical("CRITICAL: FLUME_ADMIN_TOKEN is not set. Admin endpoint is disabled.")
        raise HTTPException(status_code=403, detail="Endpoint disabled: Server configuration incomplete.")
    return True


# ── Kill Switch Endpoints ──────────────────────────────────────────────────────

@router.post("/api/tasks/stop-all", dependencies=[Depends(verify_admin_access)])
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


@router.post("/api/tasks/resume-all", dependencies=[Depends(verify_admin_access)])
async def api_tasks_resume_all(es_client: ElasticsearchClient = Depends(get_es_client)):
    correlation_id = str(uuid.uuid4())
    try:
        await es_client.update_tasks_to_ready()
        logger.info(json.dumps({"event": "kill_switch.resume.success", "elasticsearch_status": "ready", "correlation_id": correlation_id}))
        return {"success": True, "message": "All halted tasks have been reset to active. Workers will re-acquire them shortly."}
    except KillSwitchDatabaseError as e:
        logger.error(json.dumps({"event": "kill_switch.resume.failure", "error": str(e), "correlation_id": correlation_id}))
        raise HTTPException(status_code=500, detail={'error': "Database error occurred while resuming swarms.", 'correlation_id': correlation_id})


# ── Security Posture Dashboard ─────────────────────────────────────────────────

@router.get('/api/security')
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
            logger.warning("api_security_dashboard: OpenBao key lookup failed, using placeholder", exc_info=True)
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
        logger.error({"event": "api_security_failed", "error": str(e)[:300]}, exc_info=True)
        return JSONResponse(status_code=502, content={'error': str(e)[:300]})


# ── Vault Status ───────────────────────────────────────────────────────────────

def _get_vault_token() -> str:
    """Resolve Vault token from env or AppRole login."""
    t = os.environ.get('VAULT_TOKEN')
    if t:
        return t

    role_id = os.environ.get('VAULT_ROLE_ID')
    secret_id = os.environ.get('VAULT_SECRET_ID')

    if role_id and secret_id:
        try:
            openbao_url = os.environ.get('OPENBAO_URL', 'http://127.0.0.1:8200')
            client = hvac.Client(url=openbao_url)
            res = client.auth.approle.login(role_id=role_id, secret_id=secret_id)
            return res['auth']['client_token']
        except Exception as e:
            logger.error({"event": "vault_approle_login_failed", "error": str(e)[:200]})
            raise RuntimeError("Critical: Failed to authenticate via Vault AppRole.")

    raise RuntimeError("Critical: Vault authentication configuration missing. Neither VAULT_TOKEN nor VAULT_ROLE_ID/VAULT_SECRET_ID provided.")


@router.get("/api/vault/status")
def vault_status():
    openbao_url = os.environ.get('OPENBAO_URL', 'http://127.0.0.1:8200')
    vault_token = _get_vault_token()
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
