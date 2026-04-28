from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from utils.logger import get_logger

logger = get_logger("openai_oauth_state")


ENV_OAUTH_STATE_JSON = "OPENAI_OAUTH_STATE_JSON"
OPENBAO_KEY = "FLUME_CRED___openai_oauth__"

SOURCE_ENV = "env"
SOURCE_OPENBAO = "openbao"
SOURCE_MISSING = "missing"


class OpenAiOauthState(BaseModel):
    """
    Validates standard OAuth token responses (access_token, refresh_token).
    Allows extra fields gracefully since we persist dynamic 3rd-party state.
    """
    access_token: str
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None

    model_config = {"extra": "allow"}


def _get_workspace_root(state_path: Path | None) -> Path:
    if state_path:
        # e.g., state_path = workspace_root / '.agent' / 'openai_oauth_state.json'
        return state_path.parent.parent
    return Path.cwd()


def load_state_from_env_or_file(state_path: Path | None) -> tuple[dict[str, Any] | None, str]:
    # 1. Fallback lookup purely for CI/CD dynamic scripts.
    raw = (os.environ.get(ENV_OAUTH_STATE_JSON) or '').strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                model = OpenAiOauthState(**data)
                return model.model_dump(), SOURCE_ENV
        except ValidationError as e:
            logger.error(
                "Validation failed for OPENAI_OAUTH_STATE_JSON from env", 
                extra={"structured_data": {"error": str(e)}}
            )
        except Exception as e:
            logger.error(
                "Failed parsing OPENAI_OAUTH_STATE_JSON from env", 
                extra={"structured_data": {"error": str(e)}}
            )

    # 2. Main Distributed Swarm Lookup (OpenBao)
    try:
        from llm_settings import _openbao_get_all  # type: ignore
        ws_root = _get_workspace_root(state_path)
        bao_vals = _openbao_get_all(ws_root)
        bao_raw = (bao_vals.get(OPENBAO_KEY) or "").strip()
        if bao_raw:
            data = json.loads(bao_raw)
            if isinstance(data, dict):
                model = OpenAiOauthState(**data)
                return model.model_dump(), SOURCE_OPENBAO
    except ValidationError as e:
        logger.warning(
            "Validation failed for OpenAI OAuth state from OpenBao", 
            extra={"structured_data": {"error": str(e)}}
        )
    except Exception as e:
        logger.warning(
            "Failed to fetch OpenAI OAuth state from OpenBao", 
            extra={"structured_data": {"error": str(e)}}
        )
    return None, SOURCE_MISSING


def save_state_to_env_or_file(state: dict[str, Any], state_path: Path | None) -> tuple[str, str | None]:
    try:
        model = OpenAiOauthState(**state)
    except ValidationError as e:
        logger.error(
            "Invalid OpenAI OAuth state structure during save", 
            extra={"structured_data": {"error": str(e)}}
        )
        raise ValueError(f"Invalid OAuth state: {e}")

    # Use the Pydantic-validated payload (ensures keys are correct and cleans up types)
    raw = json.dumps(model.model_dump(), indent=2)
    ws_root = _get_workspace_root(state_path)
    persisted_openbao = False

    # Route raw JSON directly into centralized credential store
    try:
        from llm_settings import _openbao_put_many  # type: ignore
        _openbao_put_many(ws_root, {OPENBAO_KEY: raw})
        persisted_openbao = True
    except Exception as e:
        logger.error(
            "Failed to write OpenAI OAuth state to OpenBao vault", 
            extra={"structured_data": {"error": str(e)}}
        )

    # Legacy mapping for immediate memory sync
    os.environ[ENV_OAUTH_STATE_JSON] = raw
    
    if persisted_openbao:
        return SOURCE_OPENBAO, None
    return SOURCE_ENV, None
