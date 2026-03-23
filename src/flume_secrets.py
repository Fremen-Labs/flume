import os
import json
import logging
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("flume_secrets")

class FlumeSettings(BaseSettings):
    LLM_PROVIDER: str = "exo"
    LLM_MODEL: str = "qwen3-30b-A3B-4bit"
    LLM_BASE_URL: str = "http://host.docker.internal:52415/v1"
    LLM_API_KEY: str = ""
    GIT_USER_NAME: str = "FlumeAgent"
    GIT_USER_EMAIL: str = "agent@flume.local"
    ES_URL: str = "http://elasticsearch:9200"
    ES_API_KEY: str = ""
    ES_VERIFY_TLS: str = "false"
    OPENBAO_ADDR: str = "http://openbao:8200"
    OPENBAO_TOKEN: str = ""
    DASHBOARD_HOST: str = "0.0.0.0"
    DASHBOARD_PORT: int = 8765
    WORKER_MANAGER_POLL_SECONDS: int = 2
    WORKERS_PER_ROLE: int | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow",
    )

FLUME_ENV_KEYS = frozenset(FlumeSettings.model_fields.keys())

def load_toml_config() -> dict[str, Any]:
    """Reads structured parameters from config.toml safely bridging Docker constraints."""
    config_path = os.environ.get("FLUME_CONFIG", "config.toml")
    data = {}
    if os.path.exists(config_path):
        try:
            import tomllib
            with open(config_path, "rb") as f:
                raw = tomllib.load(f)
            if 'llm' in raw:
                data['LLM_PROVIDER'] = raw['llm'].get('provider', 'exo')
                data['LLM_MODEL'] = raw['llm'].get('model', 'qwen3-30b-A3B-4bit')
                data['LLM_BASE_URL'] = raw['llm'].get('base_url', '')
                data['LLM_API_KEY'] = raw['llm'].get('api_key', '')
            if 'git' in raw:
                data['GIT_USER_NAME'] = raw['git'].get('user', 'FlumeAgent')
                data['GIT_USER_EMAIL'] = raw['git'].get('email', '')
            if 'system' in raw:
                data['ES_URL'] = raw['system'].get('es_url', 'http://elasticsearch:9200')
                data['ES_API_KEY'] = raw['system'].get('es_api_key', '')
                data['OPENBAO_ADDR'] = raw['system'].get('openbao_url', 'http://openbao:8200')
                data['OPENBAO_TOKEN'] = raw['system'].get('openbao_token', '')
                
        except Exception as e:
            logger.warning(f"Failed parsing TOML config {config_path}: {e}")
    return data

settings = FlumeSettings(**load_toml_config())

def apply_runtime_config(workspace_root: Path | None = None) -> None:
    """Invoked globally by Flume daemon servers applying deterministic limits."""
    for key, value in settings.model_dump().items():
        if value is not None and str(value).strip():
            os.environ[key] = str(value)

def load_legacy_dotenv_into_environ(workspace_root: Path) -> None:
    """Stub mapping bounding legacy calls safely inside server.py."""
    pass

def fetch_openbao_kv(addr: str, token: str, mount: str, path: str) -> dict[str, str] | None:
    """Natively executes Vault queries strictly out of the box dynamically."""
    url = f"{addr}/v1/{mount}/data/{path}"
    req = urllib.request.Request(url, headers={"X-Vault-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            res = json.loads(r.read())
            res_data = res.get("data", {}).get("data", {})
            if not isinstance(res_data, dict):
                res_data = {}
            
            # --- Native Elasticsearch Security Auditing ---
            es_url = res_data.get("ES_URL") or os.environ.get("ES_URL", "")
            es_key = res_data.get("ES_API_KEY") or os.environ.get("ES_API_KEY", "")
            if es_url and es_key:
                import time
                import ssl
                audit_doc = {
                    "@timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "message": f"OpenBao KV securely accessed at {mount}/{path}",
                    "agent_roles": os.environ.get("FLUME_PROCESS_ROLE", "system"),
                    "worker_name": os.environ.get("WORKER_NAME", "daemon"),
                    "secret_path": f"{mount}/{path}",
                    "keys_retrieved": list(res_data.keys())
                }
                try:
                    audit_req = urllib.request.Request(
                        f"{es_url.rstrip('/')}/agent-security-audits/_doc",
                        data=json.dumps(audit_doc).encode("utf-8"),
                        headers={"Content-Type": "application/json", "Authorization": f"ApiKey {es_key}"},
                        method="POST"
                    )
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    with urllib.request.urlopen(audit_req, timeout=5, context=ctx) as audit_res:
                        if audit_res.status not in (200, 201):
                            logger.warning(f"Security audit log dropped natively: {audit_res.status}")
                except Exception as audit_e:
                    logger.warning(f"Failed to post OpenBao security checkout audit to Elasticsearch: {audit_e}")
            # ----------------------------------------------
            
            return res_data
    except Exception as e:
        logger.warning(f"Error fetching from OpenBao at {url}: {e}")
        return None

def resolve_oauth_state_path(workspace_root: Path, state_file: str = "") -> Path:
    return workspace_root / '.agent' / 'openai_oauth_state.json'
