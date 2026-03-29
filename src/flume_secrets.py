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
    ES_URL: str = "http://localhost:9200" if os.environ.get("FLUME_NATIVE_MODE") == "1" else "http://elasticsearch:9200"
    ES_API_KEY: str = ""
    ES_VERIFY_TLS: str = "false"
    OPENBAO_ADDR: str = "http://localhost:8200" if os.environ.get("FLUME_NATIVE_MODE") == "1" else "http://openbao:8200"
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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            env_settings,
            dotenv_settings,
            init_settings,
            file_secret_settings,
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
                if 'provider' in raw['llm']: data['LLM_PROVIDER'] = raw['llm']['provider']
                if 'model' in raw['llm']: data['LLM_MODEL'] = raw['llm']['model']
                if 'base_url' in raw['llm']: data['LLM_BASE_URL'] = raw['llm']['base_url']
                if 'api_key' in raw['llm']: data['LLM_API_KEY'] = raw['llm']['api_key']
            if 'git' in raw:
                if 'user' in raw['git']: data['GIT_USER_NAME'] = raw['git']['user']
                if 'email' in raw['git']: data['GIT_USER_EMAIL'] = raw['git']['email']
            if 'system' in raw:
                # Explicitly blocking the legacy docker defaults from hijacking OS arrays natively!
                if 'es_url' in raw['system']: data['ES_URL'] = raw['system']['es_url']
                if 'es_api_key' in raw['system']: data['ES_API_KEY'] = raw['system']['es_api_key']
                if 'openbao_url' in raw['system']: data['OPENBAO_ADDR'] = raw['system']['openbao_url']
                if 'openbao_token' in raw['system']: data['OPENBAO_TOKEN'] = raw['system']['openbao_token']
        except Exception as e:
            logger.warning(f"Failed parsing TOML config {config_path}: {e}")
    
    # Absolute override: If TOML explicitly mapped to docker, and we are native, enforce localhost regardless recursively
    if os.environ.get("FLUME_NATIVE_MODE") == "1":
        if "ES_URL" in data and "elasticsearch" in data["ES_URL"]:
            data["ES_URL"] = "http://localhost:9200"
        if "OPENBAO_ADDR" in data and "openbao" in data["OPENBAO_ADDR"]:
            data["OPENBAO_ADDR"] = "http://localhost:8200"
            
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
                
                max_retries = 5
                for attempt in range(max_retries):
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
                        break # Escape the loop natively upon successful index propagation
                    except (urllib.error.URLError, ConnectionError, TimeoutError) as audit_e:
                        if attempt < max_retries - 1:
                            logger.info(f"Elasticsearch actively bootstrapping. Retrying audit payload natively in 2s (Attempt {attempt + 1}/{max_retries})...")
                            time.sleep(2)
                        else:
                            logger.warning(f"Failed to post OpenBao security checkout audit to Elasticsearch after {max_retries} attempts natively: {audit_e}")
                    except Exception as audit_e:
                        logger.warning(f"Fatal Elasticsearch error evaluating native boundaries: {audit_e}")
                        break
            # ----------------------------------------------
            
            return res_data
    except Exception as e:
        logger.warning(f"Error fetching from OpenBao at {url}: {e}")
        return None

def resolve_oauth_state_path(workspace_root: Path, state_file: str = "") -> Path:
    return workspace_root / '.agent' / 'openai_oauth_state.json'

def hydrate_secrets_from_openbao() -> None:
    """Natively bridges centralized OpenBao ingestion mapping strict JSON observability traces (PR 65 Compliance)."""
    addr = os.environ.get("OPENBAO_ADDR", "http://openbao:8200")
    token = os.environ.get("OPENBAO_TOKEN", "")
    
    if not token:
        role_id = os.environ.get("VAULT_ROLE_ID")
        secret_id = os.environ.get("VAULT_SECRET_ID")
        if role_id and secret_id:
            try:
                import hvac
                client = hvac.Client(url=addr)
                res = client.auth.approle.login(role_id=role_id, secret_id=secret_id)
                token = res['auth']['client_token']
                os.environ["OPENBAO_TOKEN"] = token
            except Exception as e:
                logger.warning(f"AppRole native fetch failed: {e}")

    if not token or not addr:
        logger.warning(json.dumps({
            "event": "openbao_config_missing",
            "message": "Explicit OPENBAO_TOKEN natively missing from environment. Skipping hydration."
        }))
        return
        
    logger.info(json.dumps({
        "event": "openbao_fetch_attempt",
        "addr": addr,
        "mount": "secret",
        "path": "flume/keys"
    }))
    
    data = fetch_openbao_kv(addr, token, "secret", "flume/keys")
    if data and "ES_API_KEY" in data:
        os.environ["ES_API_KEY"] = data["ES_API_KEY"]
        logger.info(json.dumps({
            "event": "openbao_fetch_success",
            "message": "Vault seamlessly unlocked over HTTP layer.",
            "keys_hydrated": list(data.keys())
        }))
    else:
        logger.warning(json.dumps({
            "event": "openbao_fetch_failure",
            "message": "Vault returned empty structures or unreachable configurations cleanly bypassing crashes."
        }))
