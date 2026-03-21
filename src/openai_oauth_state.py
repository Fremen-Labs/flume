from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _json_from_env() -> dict[str, Any] | None:
    raw = (os.environ.get('OPENAI_OAUTH_STATE_JSON') or '').strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def load_state_from_env_or_file(state_path: Path | None) -> tuple[dict[str, Any] | None, str]:
    data = _json_from_env()
    if data is not None:
        return data, 'env'
    if state_path and state_path.exists():
        try:
            obj = json.loads(state_path.read_text())
            if isinstance(obj, dict):
                return obj, 'file'
        except Exception:
            return None, 'file'
    return None, 'missing'


def save_state_to_env_or_file(state: dict[str, Any], state_path: Path | None) -> tuple[str, str | None]:
    raw = json.dumps(state, indent=2)
    if (os.environ.get('OPENAI_OAUTH_STATE_JSON') or '').strip():
        os.environ['OPENAI_OAUTH_STATE_JSON'] = raw
        return 'env', None
    if state_path is None:
        os.environ['OPENAI_OAUTH_STATE_JSON'] = raw
        return 'env', None
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(raw)
    try:
        state_path.chmod(0o600)
    except OSError:
        pass
    return 'file', str(state_path)
