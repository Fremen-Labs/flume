import functools
from pydantic_settings import BaseSettings, SettingsConfigDict
from utils.workspace import resolve_safe_workspace

class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    ES_URL: str = "http://localhost:9200"
    ES_API_KEY: str = ""
    ES_CA_CERTS: str = ""
    FLUME_ADMIN_TOKEN: str = ""
    FLUME_WORKSPACE: str = str(resolve_safe_workspace())

@functools.lru_cache(maxsize=1)
def get_settings():
    return AppConfig()
