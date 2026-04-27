import functools
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from utils.workspace import resolve_safe_workspace

class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    
    # Core Infrastructure
    ES_URL: str = "http://localhost:9200"
    ES_API_KEY: str = ""
    ES_CA_CERTS: str = ""
    FLUME_ELASTIC_PASSWORD: str = ""
    ES_VERIFY_TLS: bool = False
    
    OPENBAO_URL: str = Field(default="http://localhost:8200", alias="OPENBAO_ADDR")
    VAULT_TOKEN: str = ""
    VAULT_ROLE_ID: str = ""
    VAULT_SECRET_ID: str = ""
    
    # Networking & Gateways
    DASHBOARD_HOST: str = "0.0.0.0"
    DASHBOARD_PORT: int = 8765
    FLUME_CORS_ORIGINS: str = ""
    GATEWAY_URL: str = Field(default="http://gateway:8090", alias="FLUME_GATEWAY_URL")
    
    # Execution Environment
    FLUME_NATIVE_MODE: str = "0"
    FLUME_WORKSPACE: str = str(resolve_safe_workspace())
    FLUME_ADMIN_TOKEN: str = ""
    FLUME_JSON_LOGS: bool = False
    
    # Telemetry & Cost
    FLUME_COST_PER_1K_INPUT: float = 0.002
    FLUME_COST_PER_1K_OUTPUT: float = 0.010
    
    # Defaults
    LLM_BASE_URL: str = "http://localhost:11434"
    LLM_MODEL: str = "llama3.2"
    LLM_PROVIDER: str = ""
    LLM_API_KEY: str = ""
    OPENAI_OAUTH_SCOPES: str = ""
    OPENAI_OAUTH_STATE_FILE: str = ""
    OPENAI_OAUTH_STATE_JSON: str = ""
    FLUME_PLANNER_MODEL: str = ""
    FLUME_FAST_MODEL: str = "o3-mini"
    FLUME_PLANNER_TIMEOUT_SECONDS: int = 300
    FLUME_PLANNER_USE_CODEX_APP_SERVER: str = "auto"
    EXO_STATUS_URL: str = "http://host.docker.internal:52415/models"
    FLUME_DATA_DIR: str = "/app"
    FLUME_CODEX_WS_PROXY: str = "1"
    FLUME_CODEX_WS_PROXY_BIND: str = ""
    FLUME_CODEX_WS_PROXY_HOST: str = ""
    FLUME_CODEX_WS_PROXY_PORT: str = "8766"
    LOOM_WORKSPACE: str = ""

@functools.lru_cache(maxsize=1)
def get_settings():
    # Allow GATEWAY_URL to fallback to GATEWAY_URL env if FLUME_GATEWAY_URL alias missing
    conf = AppConfig()
    if not os.environ.get("FLUME_GATEWAY_URL") and os.environ.get("GATEWAY_URL"):
        conf.GATEWAY_URL = os.environ["GATEWAY_URL"]
    return conf
