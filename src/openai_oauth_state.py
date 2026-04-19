from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from utils.logger import get_logger

logger = get_logger("openai_oauth_state")


def _get_workspace_root(state_path: Path | None) -> Path:
    if state_path:
        # e.g., state_path = workspace_root / '.agent' / 'openai_oauth_state.json'
        return state_path.parent.parent
    return Path.cwd()


def load_state_from_env_or_file(state_path: Path | None) -> tuple[dict[str, Any] | None, str]:
    # 1. Fallback lookup purely for CI/CD dynamic scripts.
    raw = (os.environ.get('OPENAI_OAUTH_STATE_JSON') or '').strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data, 'env'
        except Exception:
            logger.error("Failed parsing OPENAI_OAUTH_STATE_JSON from env")

    # 2. Main Distributed Swarm Lookup (OpenBao)
    try:
        from llm_settings import _openbao_get_all  # type: ignore
        ws_root = _get_workspace_root(state_path)
        bao_vals = _openbao_get_all(ws_root)
        bao_raw = (bao_vals.get("FLUME_CRED___openai_oauth__") or "").strip()
        if bao_raw:
            data = json.loads(bao_raw)
            if isinstance(data, dict):
                return data, 'openbao'
    except Exception as e:
        logger.warning(
            "Failed to fetch OpenAI OAuth state from OpenBao", 
            extra={"structured_data": {"error": str(e)}}
        )
    return None, 'missing'


def save_state_to_env_or_file(state: dict[str, Any], state_path: Path | None) -> tuple[str, str | None]:
    raw = json.dumps(state, indent=2)
    ws_root = _get_workspace_root(state_path)
    persisted_openbao = False

    # Route raw JSON directly into centralized credential store
    try:
        from llm_settings import _openbao_put_many  # type: ignore
        _openbao_put_many(ws_root, {"FLUME_CRED___openai_oauth__": raw})
        persisted_openbao = True
    except Exception as e:
        logger.error(
            "Failed to write OpenAI OAuth state to OpenBao vault", 
            extra={"structured_data": {"error": str(e)}}
        )

    # Legacy mapping for immediate memory sync
    os.environ['OPENAI_OAUTH_STATE_JSON'] = raw
    
    if persisted_openbao:
        return 'openbao', None
    return 'env', None
