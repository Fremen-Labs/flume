import functools
from pydantic_settings import BaseSettings, SettingsConfigDict
from utils.workspace import resolve_safe_workspace

class AppConfig(BaseSettings):
    # AP-10 / P6a: env_file removed — bootstrap config comes from process env
    # (Docker/CLI injects ES_URL, FLUME_ADMIN_TOKEN, etc.) or from ES flume-settings
    # via apply_runtime_config(). No .env file is read at dashboard startup.
    model_config = SettingsConfigDict(extra="ignore")
    ES_URL: str = "http://localhost:9200"
    ES_API_KEY: str = ""
    ES_CA_CERTS: str = ""
    FLUME_ADMIN_TOKEN: str = ""
    FLUME_WORKSPACE: str = str(resolve_safe_workspace())

@functools.lru_cache(maxsize=1)
def get_settings():
    return AppConfig()
