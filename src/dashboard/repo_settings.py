# Flume repo settings — GitHub + Azure DevOps credentials
# Used by dashboard server for /api/settings/repos endpoints.

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

import ado_tokens_store as ats  # type: ignore
import github_tokens_store as gts  # type: ignore
from utils.logger import get_logger

logger = get_logger("repo_settings")

MASK = "***"

EVENT_REPO_SETTINGS_GH_UPDATED = "repo_settings.github_updated"
EVENT_REPO_SETTINGS_ADO_UPDATED = "repo_settings.ado_updated"
EVENT_REPO_SETTINGS_UPDATE_FAILED = "repo_settings.update_failed"


class RepoSettings(BaseModel):
    ghToken: str
    githubTokens: list[dict[str, Any]]
    activeGithubTokenId: str | None
    adoToken: str
    adoOrgUrl: str
    adoCredentials: list[dict[str, Any]]
    activeAdoCredentialId: str | None


class RepoSettingsResponse(BaseModel):
    settings: RepoSettings
    restartRequired: bool = False


class UpdateRepoSettingsRequest(BaseModel):
    githubTokenAction: dict[str, Any] | None = None
    adoTokenAction: dict[str, Any] | None = None
    ghToken: str | None = None
    adoToken: str | None = None
    adoOrgUrl: str | None = None


def get_repo_settings_response(workspace_root: Path) -> dict[str, Any]:
    gts.ensure_migrated_from_env(workspace_root)
    ats.ensure_migrated_from_env(workspace_root)

    active_gh = gts.get_active_token_plain(workspace_root)
    gh_mask = MASK if active_gh else ""

    active_ado_tok = ats.get_active_token_plain(workspace_root)
    ado_mask = MASK if active_ado_tok else ""
    ado_org = ats.get_active_org_url(workspace_root)

    response = RepoSettingsResponse(
        settings=RepoSettings(
            ghToken=gh_mask,
            githubTokens=gts.list_public_tokens(workspace_root),
            activeGithubTokenId=gts.get_active_token_id(workspace_root),
            adoToken=ado_mask,
            adoOrgUrl=ado_org or "",
            adoCredentials=ats.list_public_credentials(workspace_root),
            activeAdoCredentialId=ats.get_active_credential_id(workspace_root),
        ),
        restartRequired=False,
    )
    return response.model_dump()


def update_repo_settings(
    workspace_root: Path, payload: UpdateRepoSettingsRequest | dict[str, Any]
) -> tuple[bool, str]:
    if isinstance(payload, dict):
        req = UpdateRepoSettingsRequest(**payload)
    else:
        req = payload

    gts.ensure_migrated_from_env(workspace_root)
    ats.ensure_migrated_from_env(workspace_root)

    if req.githubTokenAction is not None:
        ok, err = gts.apply_action(workspace_root, req.githubTokenAction)
        if not ok:
            logger.warning("GitHub token action failed",
                           extra={"structured_data": {"event": EVENT_REPO_SETTINGS_UPDATE_FAILED, "error": err, "provider": "github"}})
            return False, err
        logger.info("GitHub token action applied",
                    extra={"structured_data": {"event": EVENT_REPO_SETTINGS_GH_UPDATED, "action": req.githubTokenAction.get("action")}})

    if req.adoTokenAction is not None:
        ok, err = ats.apply_action(workspace_root, req.adoTokenAction)
        if not ok:
            logger.warning("ADO token action failed",
                           extra={"structured_data": {"event": EVENT_REPO_SETTINGS_UPDATE_FAILED, "error": err, "provider": "ado"}})
            return False, err
        logger.info("ADO token action applied",
                    extra={"structured_data": {"event": EVENT_REPO_SETTINGS_ADO_UPDATED, "action": req.adoTokenAction.get("action")}})

    if req.ghToken is not None and not req.githubTokenAction:
        token = req.ghToken.strip()
        if token and token != MASK:
            ok, err = gts.apply_legacy_gh_token_value(workspace_root, token)
            if not ok:
                logger.warning("Legacy GitHub token update failed",
                               extra={"structured_data": {"event": EVENT_REPO_SETTINGS_UPDATE_FAILED, "error": err, "provider": "github"}})
                return False, err
            logger.info("Legacy GitHub token applied",
                        extra={"structured_data": {"event": EVENT_REPO_SETTINGS_GH_UPDATED, "action": "legacy_update"}})

    if not req.adoTokenAction:
        ut = uo = False
        tv = ov = ""
        if req.adoToken is not None:
            raw_t = req.adoToken.strip()
            if raw_t != MASK:
                ut = True
                tv = raw_t
        if req.adoOrgUrl is not None:
            raw_o = req.adoOrgUrl.strip()
            if raw_o != MASK:
                uo = True
                ov = raw_o
        if ut or uo:
            ok, err = ats.apply_legacy_patch(
                workspace_root, update_token=ut, token=tv, update_org=uo, org_url=ov
            )
            if not ok:
                logger.warning("Legacy ADO settings patch failed",
                               extra={"structured_data": {"event": EVENT_REPO_SETTINGS_UPDATE_FAILED, "error": err, "provider": "ado"}})
                return False, err
            logger.info("Legacy ADO settings applied",
                        extra={"structured_data": {"event": EVENT_REPO_SETTINGS_ADO_UPDATED, "action": "legacy_patch"}})

    return True, ""
