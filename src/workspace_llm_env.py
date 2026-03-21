"""
Keep worker processes aligned with dashboard LLM settings.

The dashboard resolves LLM_* via llm_settings.load_effective_pairs (.env + process + OpenBao).
Worker manager/handlers only ran flume_secrets.apply_runtime_config at startup, which can miss
the same merge order or leave stale os.environ. We re-sync the keys that affect agent routing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

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
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


def normalize_gemini_model_id(model_id: str | None) -> str:
    """Map deprecated Gemini API model strings to current stable IDs."""
    m = (model_id or "").strip()
    if not m:
        return DEFAULT_GEMINI_MODEL
    return _GEMINI_MODEL_ALIASES.get(m, m)

_LLM_SYNC_KEYS = (
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_BASE_URL",
    "LLM_API_KEY",
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
    gm = (global_llm_model or "").strip() or "llama3.2"
    if pid in _CLOUD_LLM_PROVIDER_IDS and sm == "llama3.2":
        out = gm
    else:
        out = sm if sm else gm
    if pid == "gemini":
        out = normalize_gemini_model_id(out)
    return out


def _inject_llm_key_from_active_credential(workspace_root: Path) -> None:
    """
    If LLM_API_KEY is still empty after load_effective_pairs, copy the key from the active
    row in worker-manager/llm_credentials.json (Settings may store keys only there).
    """
    if (os.environ.get("LLM_API_KEY") or "").strip():
        return
    prov = (os.environ.get("LLM_PROVIDER") or "ollama").strip().lower()
    if prov == "ollama":
        return
    wr = workspace_root.resolve()
    if str(wr) not in sys.path:
        sys.path.insert(0, str(wr))
    try:
        import llm_credentials_store as lcs

        aid = lcs.get_active_credential_id(wr)
        if not aid:
            return
        c = lcs.get_by_id(wr, aid)
        if not c:
            return
        cprov = str(c.get("provider") or "").strip().lower()
        ckey = str(c.get("apiKey") or "").strip()
        if not ckey or cprov != prov:
            return
        os.environ["LLM_API_KEY"] = ckey
        cbase = str(c.get("baseUrl") or "").strip()
        if cbase and prov == "openai_compatible":
            os.environ["LLM_BASE_URL"] = cbase
    except Exception:
        pass


def sync_llm_env_from_workspace(workspace_root: Path) -> None:
    """Overlay os.environ with dashboard-equivalent LLM_* from load_effective_pairs."""
    wr = workspace_root.resolve()
    dash = wr / "dashboard"
    if not dash.is_dir():
        return
    if str(wr) not in sys.path:
        sys.path.insert(0, str(wr))
    if str(dash) not in sys.path:
        sys.path.insert(0, str(dash))
    try:
        from llm_settings import load_effective_pairs

        pairs = load_effective_pairs(wr)
        for key in _LLM_SYNC_KEYS:
            v = pairs.get(key)
            if v is None:
                continue
            s = str(v).strip()
            if not s:
                # Avoid clearing API key / OAuth-derived token in env when KV has empty placeholder
                if key == "LLM_API_KEY":
                    continue
                continue
            os.environ[key] = s
        _inject_llm_key_from_active_credential(wr)
    except Exception as e:
        try:
            logf = wr / "worker-manager" / "llm_sync_errors.log"
            logf.parent.mkdir(parents=True, exist_ok=True)
            from datetime import datetime, timezone

            with logf.open("a", encoding="utf-8") as lf:
                lf.write(
                    f"{datetime.now(timezone.utc).isoformat()} sync_llm_env_from_workspace: {e!r}\n"
                )
        except OSError:
            pass
