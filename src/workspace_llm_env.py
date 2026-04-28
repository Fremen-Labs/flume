"""
AP-10: Keep worker processes aligned with cluster-native LLM settings.

The dashboard now stores LLM_* config in Elasticsearch (flume-llm-config) and
OpenBao (secrets). Workers use get_active_llm_model() to read the current model
directly from ES rather than relying on os.environ baked in at container start.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import BaseModel, ValidationError

_BS_WS = Path(__file__).resolve().parent
if str(_BS_WS) not in sys.path:
    sys.path.insert(0, str(_BS_WS))

from utils.logger import get_logger  # noqa: E402
logger = get_logger("workspace_llm_env")


ENV_LLM_MODEL = "LLM_MODEL"
ENV_LLM_PROVIDER = "LLM_PROVIDER"
ENV_LLM_API_KEY = "LLM_API_KEY"
ENV_LLM_BASE_URL = "LLM_BASE_URL"
ENV_LOCAL_OLLAMA_BASE_URL = "LOCAL_OLLAMA_BASE_URL"
ENV_FLUME_NATIVE_MODE = "FLUME_NATIVE_MODE"

PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
PROVIDER_GEMINI = "gemini"

DEFAULT_OLLAMA_MODEL = "llama3.2"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


class LlmConfigDoc(BaseModel):
    """Validates the ES configuration blob from load_llm_config()."""
    LLM_MODEL: str = ""
    model_config = {"extra": "allow"}


class ActiveCredentialDto(BaseModel):
    """Validates boundary crossing from llm_credentials_store.py."""
    provider: str = ""
    apiKey: str = ""
    baseUrl: str = ""
    model_config = {"extra": "allow"}


def get_active_llm_model(default: str = DEFAULT_OLLAMA_MODEL) -> str:
    """AP-10: Return the current LLM model from ES (flume-llm-config), falling back
    to the process-env value baked in at container start.

    This allows hot-reloading the model without a container restart.
    """
    try:
        from es_credential_store import load_llm_config
        config = load_llm_config()
        if isinstance(config, dict):
            try:
                model = LlmConfigDoc(**config)
                if model.LLM_MODEL.strip():
                    return model.LLM_MODEL.strip()
            except ValidationError as e:
                logger.warning(
                    "Invalid LLM config schema from ES",
                    extra={"structured_data": {"error": str(e)}}
                )
    except Exception:
        pass
    return (os.environ.get(ENV_LLM_MODEL) or default).strip() or default


# Hosted APIs — model id "llama3.2" is the Ollama default and is invalid here; use Settings model.
_CLOUD_LLM_PROVIDER_IDS = frozenset({"openai", "anthropic", "gemini", "xai", "mistral", "cohere"})

# Google retires old IDs on the OpenAI-compatible endpoint; map saved .env / agent_models values.
_GEMINI_MODEL_ALIASES = {
    "gemini-1.5-flash": "gemini-2.5-flash",
    "gemini-1.5-flash-latest": "gemini-2.5-flash",
    "gemini-1.5-flash-8b": "gemini-2.5-flash",
    "gemini-1.5-pro": "gemini-2.5-pro",
    "gemini-1.5-pro-latest": "gemini-2.5-pro",
    "gemini-2.0-flash": "gemini-2.5-flash",
    "gemini-2.0-flash-lite": "gemini-2.5-flash-lite",
}


def normalize_gemini_model_id(model_id: str | None) -> str:
    """Map deprecated Gemini API model strings to current stable IDs."""
    m = (model_id or "").strip()
    if not m:
        return DEFAULT_GEMINI_MODEL
    return _GEMINI_MODEL_ALIASES.get(m, m)


_LLM_SYNC_KEYS = (
    ENV_LLM_PROVIDER,
    ENV_LLM_MODEL,
    ENV_LLM_BASE_URL,
    ENV_LLM_API_KEY,
    "OPENAI_OAUTH_STATE_FILE",
    "OPENAI_OAUTH_STATE_JSON",
    "OPENAI_OAUTH_TOKEN_URL",
    "EXECUTION_HOST",
)


def resolve_cloud_agent_model(provider_id: str, stored_model: str, global_llm_model: str) -> str:
    """
    Per-role agent_models.json sometimes stores provider=openai with model=llama3.2 (stale default).
    Replace with the global Settings model for cloud providers.
    """
    pid = (provider_id or "").strip().lower()
    sm = (stored_model or "").strip()
    gm = (global_llm_model or "").strip() or DEFAULT_OLLAMA_MODEL
    if pid in _CLOUD_LLM_PROVIDER_IDS and sm == DEFAULT_OLLAMA_MODEL:
        out = gm
    else:
        out = sm if sm else gm
    if pid == PROVIDER_GEMINI:
        out = normalize_gemini_model_id(out)
    return out


def _inject_llm_key_from_active_credential(workspace_root: Path) -> None:
    """
    If LLM_API_KEY is still empty after load_effective_pairs, copy the key from the active
    row in worker-manager/llm_credentials.json (Settings may store keys only there).
    """
    if (os.environ.get(ENV_LLM_API_KEY) or "").strip():
        return
    prov = (os.environ.get(ENV_LLM_PROVIDER) or PROVIDER_OLLAMA).strip().lower()
    if prov == PROVIDER_OLLAMA:
        return
    wr = workspace_root.resolve()
    if str(wr) not in sys.path:
        sys.path.append(str(wr))
    try:
        import llm_credentials_store as lcs

        aid = lcs.get_active_credential_id(wr)
        if not aid:
            return
        c_raw = lcs.get_by_id(wr, aid)
        if not c_raw or not isinstance(c_raw, dict):
            return
            
        try:
            c = ActiveCredentialDto(**c_raw)
        except ValidationError as e:
            logger.warning(
                "Invalid credential structure for active key injection",
                extra={"structured_data": {"error": str(e), "id": aid}}
            )
            return
            
        cprov = c.provider.strip().lower()
        ckey = c.apiKey.strip()
        if not ckey or cprov != prov:
            return
        os.environ[ENV_LLM_API_KEY] = ckey
        cbase = c.baseUrl.strip()
        if cbase and prov == PROVIDER_OPENAI_COMPATIBLE:
            os.environ[ENV_LLM_BASE_URL] = cbase
    except Exception as e:
        logger.error("Agent swarm captured suppressed exception", extra={"structured_data": {"error": str(e)}})


def _is_docker_mode() -> bool:
    """True when running inside a Docker container (not native macOS/Linux dev mode)."""
    if os.environ.get(ENV_FLUME_NATIVE_MODE, "").strip() == "1":
        return False
    # Standard container detection heuristics
    return os.path.isfile("/.dockerenv") or os.path.isfile("/run/.containerenv")


def _rewrite_loopback_for_docker(url: str) -> str:
    """Replace 127.0.0.1/localhost with host.docker.internal inside Docker."""
    if not url:
        return url
    for loopback in ("://127.0.0.1", "://localhost"):
        if loopback in url:
            url = url.replace(loopback, "://host.docker.internal")
    return url


def sync_llm_env_from_workspace(workspace_root: Path) -> None:
    """Overlay os.environ with dashboard-equivalent LLM_* from load_effective_pairs."""
    wr = workspace_root.resolve()
    dash = wr / "dashboard"
    if not dash.is_dir():
        return
    if str(wr) not in sys.path:
        sys.path.append(str(wr))
    added_dash = False
    if str(dash) not in sys.path:
        sys.path.insert(0, str(dash))
        added_dash = True
    try:
        from llm_settings import load_effective_pairs

        pairs = load_effective_pairs(wr)
        if added_dash:
            sys.path.remove(str(dash))
        for key in _LLM_SYNC_KEYS:
            v = pairs.get(key)
            if v is None:
                continue
            s = str(v).strip()
            if not s:
                # Avoid clearing API key / OAuth-derived token in env when KV has empty placeholder
                if key == ENV_LLM_API_KEY:
                    continue
                continue
            os.environ[key] = s

        # Docker safety net: rewrite loopback URLs so workers can reach host services
        if _is_docker_mode():
            for url_key in (ENV_LLM_BASE_URL, ENV_LOCAL_OLLAMA_BASE_URL):
                cur = os.environ.get(url_key, "")
                if cur:
                    fixed = _rewrite_loopback_for_docker(cur)
                    if fixed != cur:
                        os.environ[url_key] = fixed
                        logger.info(
                            "Docker loopback fix",
                            extra={"structured_data": {"key": url_key, "original": cur, "fixed": fixed}}
                        )

        _inject_llm_key_from_active_credential(wr)
    except Exception as e:
        logger.error("sync_llm_env_from_workspace exception", extra={"structured_data": {"error": repr(e)}})
