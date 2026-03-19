# Flume repo settings — GitHub + Azure DevOps credentials
# Used by dashboard server for /api/settings/repos endpoints.

from __future__ import annotations

from pathlib import Path
from typing import Any

# Reuse the same settings helpers from llm_settings.py
from llm_settings import load_effective_pairs, _update_env_keys  # type: ignore

MASK = "***"

ENV_GH_TOKEN = "GH_TOKEN"
ENV_ADO_TOKEN = "ADO_TOKEN"
ENV_ADO_ORG_URL = "ADO_ORG_URL"


def _mask_if_set(value: str) -> str:
    return MASK if str(value or "").strip() else ""


def validate_repo_settings(payload: dict[str, Any], workspace_root: Path) -> tuple[bool, str, dict[str, str]]:
    """
    Validate settings payload and return (ok, error_message, env_updates).
    env_updates is the dict of key=value to write to .env.

    Notes:
    - We treat token input of "***" as "leave unchanged".
    - Empty string is treated as "set empty" (clear).
    - Missing fields are treated as "leave unchanged".
    """
    updates: dict[str, str] = {}

    if "ghToken" in payload:
        token = str(payload.get("ghToken") or "").strip()
        if token != MASK:
            updates[ENV_GH_TOKEN] = token

    if "adoToken" in payload:
        token = str(payload.get("adoToken") or "").strip()
        if token != MASK:
            updates[ENV_ADO_TOKEN] = token

    if "adoOrgUrl" in payload:
        url = str(payload.get("adoOrgUrl") or "").strip()
        if url != MASK:
            updates[ENV_ADO_ORG_URL] = url

    return True, "", updates


def get_repo_settings_response(workspace_root: Path) -> dict[str, Any]:
    pairs = load_effective_pairs(workspace_root)

    return {
        "settings": {
            "ghToken": _mask_if_set(pairs.get(ENV_GH_TOKEN, "")),
            "adoToken": _mask_if_set(pairs.get(ENV_ADO_TOKEN, "")),
            "adoOrgUrl": str(pairs.get(ENV_ADO_ORG_URL, "") or "").strip(),
        },
        "restartRequired": True,
    }


def update_repo_settings(workspace_root: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    ok, err, updates = validate_repo_settings(payload, workspace_root)
    if not ok:
        return False, err
    if not updates:
        # Nothing to update; still considered ok.
        return True, ""
    _update_env_keys(workspace_root, updates)
    return True, ""

