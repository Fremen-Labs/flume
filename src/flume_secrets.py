import os
import json
import urllib.request
import urllib.error
import sys
from pathlib import Path
from typing import Any
from pydantic_settings import BaseSettings, SettingsConfigDict

_BS_WS = Path(__file__).resolve().parent
if str(_BS_WS) not in sys.path:
    sys.path.insert(0, str(_BS_WS))

from utils.logger import get_logger  # noqa: E402
logger = get_logger("flume_secrets")

class FlumeSettings(BaseSettings):
    LLM_PROVIDER: str = "ollama"
    LLM_MODEL: str = "llama3.2"
    LLM_BASE_URL: str = "http://host.docker.internal:11434"
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

    # AP-10 / P6a: env_file removed — all values come from process env (Docker
    # compose / CLI injects bootstrap vars) or are overlaid by apply_runtime_config()
    # which reads from ES flume-settings + OpenBao. No .env file is read at startup.
    model_config = SettingsConfigDict(
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

def load_elastic_config() -> dict[str, Any]:
    """Reads configuration natively from Elasticsearch bounding ephemeral pod orchestration."""
    data = {}
    
    # Elasticsearch connection bounding
    es_native = os.environ.get("FLUME_NATIVE_MODE") == "1"
    es_url = "http://localhost:9200" if es_native else "http://elasticsearch:9200"
    
    # Query system settings from elastic cluster natively
    try:
        req = urllib.request.Request(f"{es_url}/flume-settings/_doc/system")
        with urllib.request.urlopen(req, timeout=1.5) as r:
            res = json.loads(r.read())
            doc = res.get("_source", {})
            if "es_url" in doc:
                data["ES_URL"] = doc["es_url"]
            if "es_api_key" in doc and doc["es_api_key"] != "***":
                data["ES_API_KEY"] = doc["es_api_key"]
            if "openbao_url" in doc:
                data["OPENBAO_ADDR"] = doc["openbao_url"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.debug("flume-settings not found, using environment defaults")
        else:
            logger.warning(f"Failed to bootstrap configuration natively from Elasticsearch: HTTP Error {e.code}")
    except Exception as e:
        logger.warning(f"Failed to bootstrap configuration natively from Elasticsearch: {e}")

    if es_native:
        if "ES_URL" in data and "elasticsearch" in data["ES_URL"]:
            data["ES_URL"] = "http://localhost:9200"
        if "OPENBAO_ADDR" in data and "openbao" in data["OPENBAO_ADDR"]:
            data["OPENBAO_ADDR"] = "http://localhost:8200"
            
    return data

settings = FlumeSettings(**load_elastic_config())

# AP-10: LLM settings are owned by ES (flume-llm-config) and OpenBao.
# These keys must NOT be applied from FlumeSettings (which reads stale docker-compose env).
_CLUSTER_OWNED_KEYS = frozenset({
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "OPENAI_OAUTH_STATE_FILE",
    "OPENAI_OAUTH_STATE_JSON",
    "OPENAI_OAUTH_TOKEN_URL",
})

def apply_runtime_config(workspace_root: Path | None = None) -> None:
    """Apply FlumeSettings to os.environ, skipping LLM keys (those come from ES/OpenBao)."""
    for key, value in settings.model_dump().items():
        if key in _CLUSTER_OWNED_KEYS:
            # LLM config is cluster-native: skip FlumeSettings values which may be stale
            # docker-compose injection. ES (flume-llm-config) overlay happens below.
            continue
        if value is not None and str(value).strip():
            os.environ[key] = str(value)

    # After base config, overlay LLM settings from ES (source of truth)
    try:
        _src = str(Path(__file__).resolve().parent)
        if _src not in sys.path:
            sys.path.insert(0, _src)
        import es_credential_store as _esc
        _es_config = _esc.load_llm_config()
        for _k, _v in _es_config.items():
            if _v and str(_v).strip():
                os.environ[_k] = str(_v).strip()
    except Exception:
        pass  # Graceful degradation: keep docker-compose values if ES is unreachable


def load_legacy_dotenv_into_environ(workspace_root: Path) -> None:
    """AP-10: No-op stub. .env is bootstrap-only; LLM keys come from ES/OpenBao."""
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
    """Hydrate os.environ with secrets from OpenBao KV (secret/flume/keys).

    Bootstrap order for OPENBAO_TOKEN:
      1. OPENBAO_TOKEN process env (Docker/CLI injects this at container start)
      2. AppRole login using VAULT_ROLE_ID + BAO_SECRET_ID from process env or
         ES flume-settings (written by CLI SeedBootstrapConfig after provisioning).
    """
    addr = os.environ.get("OPENBAO_ADDR", "http://openbao:8200")
    token = os.environ.get("OPENBAO_TOKEN", "")

    if not token:
        role_id = os.environ.get("VAULT_ROLE_ID")
        secret_id = os.environ.get("VAULT_SECRET_ID", "")

        # AP-10 / P6a: BAO_SECRET_ID is written to ES flume-settings by the CLI
        # SeedBootstrapConfig() after AppRole provisioning. Read from ES instead
        # of the former /app/.env file path (which no longer exists).
        if not secret_id:
            try:
                es_config = load_elastic_config()
                secret_id = es_config.get("BAO_SECRET_ID", "").strip()
                if secret_id:
                    logger.debug("Loaded BAO_SECRET_ID from ES flume-settings")
                else:
                    logger.debug("BAO_SECRET_ID not found in ES flume-settings; AppRole login skipped")
            except Exception as e:
                logger.warning(f"Failed to load BAO_SECRET_ID from ES flume-settings: {e}")

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
    if not data:
        logger.warning(json.dumps({
            "event": "openbao_fetch_failure",
            "message": "Vault returned empty structures or unreachable configurations cleanly bypassing crashes."
        }))
        return

    # Apply ALL keys from OpenBao KV to os.environ — this makes OpenBao the
    # single source of truth for SECRETS, eliminating .env file dependency.
    # Non-sensitive LLM settings MUST NOT be applied here to ensure ES remains the source of truth.
    hydrated_keys = []
    _es_keys = frozenset({"LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_ROUTE_TYPE"})
    for key, value in data.items():
        if key in _es_keys:
            continue
        if value is not None and str(value).strip():
            os.environ[key] = str(value).strip()
            hydrated_keys.append(key)

    logger.info(json.dumps({
        "event": "openbao_fetch_success",
        "message": "Vault seamlessly unlocked over HTTP layer.",
        "keys_hydrated": hydrated_keys
    }))
