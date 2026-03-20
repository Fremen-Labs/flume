# Flume repo settings — GitHub + Azure DevOps credentials
# Used by dashboard server for /api/settings/repos endpoints.

from __future__ import annotations

from pathlib import Path
from typing import Any

# Reuse the same settings helpers from llm_settings.py
from llm_settings import load_effective_pairs, _update_env_keys  # type: ignore

import github_tokens_store as gts  # type: ignore

MASK = "***"

ENV_ADO_TOKEN = "ADO_TOKEN"
ENV_ADO_ORG_URL = "ADO_ORG_URL"


def validate_repo_settings(payload: dict[str, Any], workspace_root: Path) -> tuple[bool, str, dict[str, str]]:
    """
    Validate settings payload and return (ok, error_message, env_updates).
    env_updates is the dict of key=value to write to .env.

    Notes:
    - We treat token input of "***" as "leave unchanged".
    - Empty string is treated as "set empty" (clear).
    - Missing fields are treated as "leave unchanged".
    - GH_TOKEN is managed via github_tokens_store (not through this dict).
    """
    updates: dict[str, str] = {}

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
    gts.ensure_migrated_from_env(workspace_root)
    pairs = load_effective_pairs(workspace_root)

    active_plain = gts.get_active_token_plain(workspace_root)
    gh_mask = MASK if active_plain else ""

    return {
        "settings": {
            "ghToken": gh_mask,
            "githubTokens": gts.list_public_tokens(workspace_root),
            "activeGithubTokenId": gts.get_active_token_id(workspace_root),
            "adoToken": _mask_if_set(pairs.get(ENV_ADO_TOKEN, "")),
            "adoOrgUrl": str(pairs.get(ENV_ADO_ORG_URL, "") or "").strip(),
        },
        "restartRequired": True,
    }


def _mask_if_set(value: str) -> str:
    return MASK if str(value or "").strip() else ""


def update_repo_settings(workspace_root: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    gts.ensure_migrated_from_env(workspace_root)

    if isinstance(payload.get("githubTokenAction"), dict):
        ok, err = gts.apply_action(workspace_root, payload["githubTokenAction"])
        if not ok:
            return False, err

    action_sent = isinstance(payload.get("githubTokenAction"), dict)
    if "ghToken" in payload and not action_sent:
        token = str(payload.get("ghToken") or "").strip()
        if token and token != MASK:
            ok, err = gts.apply_legacy_gh_token_value(workspace_root, token)
            if not ok:
                return False, err

    ok, err, updates = validate_repo_settings(payload, workspace_root)
    if not ok:
        return False, err
    if not updates:
        return True, ""
    _update_env_keys(workspace_root, updates)
    return True, ""
