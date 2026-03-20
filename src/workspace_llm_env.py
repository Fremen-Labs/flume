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

_LLM_SYNC_KEYS = (
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "OPENAI_OAUTH_STATE_FILE",
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
        return gm
    return sm if sm else gm


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
    except Exception:
        pass
