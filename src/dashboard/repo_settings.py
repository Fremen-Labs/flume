# Flume repo settings — GitHub + Azure DevOps credentials
# Used by dashboard server for /api/settings/repos endpoints.

from __future__ import annotations

from pathlib import Path
from typing import Any

# Reuse the same settings helpers from llm_settings.py
from llm_settings import load_effective_pairs  # type: ignore

import ado_tokens_store as ats  # type: ignore
import github_tokens_store as gts  # type: ignore

MASK = "***"


def get_repo_settings_response(workspace_root: Path) -> dict[str, Any]:
    gts.ensure_migrated_from_env(workspace_root)
    ats.ensure_migrated_from_env(workspace_root)
    pairs = load_effective_pairs(workspace_root)

    active_gh = gts.get_active_token_plain(workspace_root)
    gh_mask = MASK if active_gh else ""

    active_ado_tok = ats.get_active_token_plain(workspace_root)
    ado_mask = MASK if active_ado_tok else ""
    ado_org = ats.get_active_org_url(workspace_root)

    return {
        "settings": {
            "ghToken": gh_mask,
            "githubTokens": gts.list_public_tokens(workspace_root),
            "activeGithubTokenId": gts.get_active_token_id(workspace_root),
            "adoToken": ado_mask,
            "adoOrgUrl": ado_org,
            "adoCredentials": ats.list_public_credentials(workspace_root),
            "activeAdoCredentialId": ats.get_active_credential_id(workspace_root),
        },
        "restartRequired": True,
    }


def update_repo_settings(workspace_root: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    gts.ensure_migrated_from_env(workspace_root)
    ats.ensure_migrated_from_env(workspace_root)

    if isinstance(payload.get("githubTokenAction"), dict):
        ok, err = gts.apply_action(workspace_root, payload["githubTokenAction"])
        if not ok:
            return False, err

    if isinstance(payload.get("adoTokenAction"), dict):
        ok, err = ats.apply_action(workspace_root, payload["adoTokenAction"])
        if not ok:
            return False, err

    gh_action = isinstance(payload.get("githubTokenAction"), dict)
    ado_action = isinstance(payload.get("adoTokenAction"), dict)

    if "ghToken" in payload and not gh_action:
        token = str(payload.get("ghToken") or "").strip()
        if token and token != MASK:
            ok, err = gts.apply_legacy_gh_token_value(workspace_root, token)
            if not ok:
                return False, err

    if not ado_action:
        ut = uo = False
        tv = ov = ""
        if "adoToken" in payload:
            raw_t = str(payload.get("adoToken") or "").strip()
            if raw_t != MASK:
                ut = True
                tv = raw_t
        if "adoOrgUrl" in payload:
            raw_o = str(payload.get("adoOrgUrl") or "").strip()
            if raw_o != MASK:
                uo = True
                ov = raw_o
        if ut or uo:
            ok, err = ats.apply_legacy_patch(
                workspace_root, update_token=ut, token=tv, update_org=uo, org_url=ov
            )
            if not ok:
                return False, err

    return True, ""
