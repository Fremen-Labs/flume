# Per-role agent model + execution host — persisted beside worker state.
# Each role can pick a saved LLM credential (labeled API key + provider) and a model.

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import llm_credentials_store as lcs
from llm_settings import PROVIDER_CATALOG, load_effective_pairs, get_oauth_status
from workspace_llm_env import resolve_cloud_agent_model

AGENT_ROLE_IDS = (
    "intake",
    "pm",
    "implementer",
    "tester",
    "reviewer",
    "memory-updater",
)


def agent_models_path(workspace_root: Path) -> Path:
    return workspace_root / "worker-manager" / "agent_models.json"


def _catalog_entry(provider_id: str) -> Optional[dict[str, Any]]:
    for p in PROVIDER_CATALOG:
        if p["id"] == provider_id:
            return p
    return None


def _provider_is_configured_env(workspace_root: Path, provider_id: str, pairs: dict[str, str]) -> bool:
    """Whether LLM_* env / OpenBao supports this provider (legacy / Settings default)."""
    pid = provider_id.strip().lower()
    current = pairs.get("LLM_PROVIDER", "ollama").strip().lower()
    api_key = pairs.get("LLM_API_KEY", "").strip()
    base_url = pairs.get("LLM_BASE_URL", "").strip()

    if pid == "ollama":
        return True

    if pid == "openai":
        if current != "openai":
            return False
        if api_key:
            return True
        try:
            return bool(get_oauth_status(workspace_root).get("configured"))
        except Exception:
            return False

    if pid == "openai_compatible":
        return current == "openai_compatible" and bool(api_key) and bool(base_url)

    if pid in ("anthropic", "gemini", "xai", "mistral", "cohere"):
        return current == pid and bool(api_key)

    return False


def provider_is_configured(
    workspace_root: Path,
    provider_id: str,
    pairs: dict[str, str],
    credential_id: Optional[str] = None,
) -> bool:
    """Whether this provider can be used for agent calls."""
    pid = provider_id.strip().lower()
    cid = (credential_id or "").strip()

    if cid == lcs.OLLAMA_CREDENTIAL_ID:
        return pid == "ollama"

    if cid and cid not in ("", lcs.SETTINGS_DEFAULT_CREDENTIAL_ID):
        cred = lcs.get_by_id(workspace_root, cid)
        if not cred:
            return False
        cprov = str(cred.get("provider") or "").strip().lower()
        if cprov != pid:
            return False
        key = str(cred.get("apiKey") or "").strip()
        if pid == "openai_compatible":
            return bool(key) and bool(str(cred.get("baseUrl") or "").strip())
        if pid == "openai":
            return bool(key)
        if pid == "ollama":
            return True
        return bool(key)

    return _provider_is_configured_env(workspace_root, pid, pairs)


def available_credentials_for_agents(workspace_root: Path) -> list[dict[str, Any]]:
    """
    Selectable credentials for the Agents UI: Settings default profile, saved keys, Ollama.
    """
    pairs = load_effective_pairs(workspace_root)
    out: list[dict[str, Any]] = []

    current = pairs.get("LLM_PROVIDER", "ollama").strip().lower()
    if current != "ollama" and _provider_is_configured_env(workspace_root, current, pairs):
        entry = _catalog_entry(current)
        models = list((entry or {}).get("models") or [])
        if current == "openai_compatible":
            models = []
        out.append(
            {
                "credentialId": lcs.SETTINGS_DEFAULT_CREDENTIAL_ID,
                "label": f"Active Settings ({entry.get('name', current) if entry else current})",
                "shortLabel": "Settings (default)",
                "providerId": current,
                "configured": True,
                "models": models,
                "allowCustomModelId": current in ("ollama", "openai_compatible"),
            }
        )

    for c in lcs.list_public_credentials(workspace_root):
        if not c.get("hasKey"):
            continue
        pid = str(c.get("provider") or "").strip().lower()
        entry = _catalog_entry(pid)
        if not entry and pid != "openai_compatible":
            continue
        base = str(c.get("baseUrl") or "").strip()
        if pid == "openai_compatible" and not base:
            continue
        models = list((entry or {}).get("models") or []) if entry else []
        if pid == "openai_compatible":
            models = []
        disp = entry.get("name", pid) if entry else pid
        out.append(
            {
                "credentialId": c["id"],
                "label": f"{c['label']} · {disp}",
                "shortLabel": c["label"],
                "providerId": pid,
                "configured": True,
                "models": models,
                "allowCustomModelId": pid in ("ollama", "openai_compatible"),
            }
        )

    ollama_entry = _catalog_entry("ollama")
    if ollama_entry:
        out.append(
            {
                "credentialId": lcs.OLLAMA_CREDENTIAL_ID,
                "label": ollama_entry.get("name", "Ollama (local)"),
                "shortLabel": "Ollama",
                "providerId": "ollama",
                "configured": True,
                "models": list(ollama_entry.get("models") or []),
                "allowCustomModelId": True,
                "hint": "Uses LLM_BASE_URL / default :11434",
            }
        )

    return out


def _credential_group_by_id(groups: list[dict[str, Any]], cred_id: str) -> Optional[dict[str, Any]]:
    for g in groups:
        if g.get("credentialId") == cred_id:
            return g
    return None


def _custom_model_ok(provider_id: str, model_id: str) -> bool:
    mid = model_id.strip()
    if not mid or len(mid) > 200:
        return False
    if not re.match(r"^[\w.\-:/]+$", mid):
        return False
    return provider_id in ("ollama", "openai_compatible")


def _role_model_allowed(groups: list[dict[str, Any]], cred_id: str, model: str) -> bool:
    g = _credential_group_by_id(groups, cred_id)
    if not g:
        return False
    prov = g["providerId"]
    if g.get("allowCustomModelId"):
        return _custom_model_ok(prov, model)
    for m in g.get("models") or []:
        if str(m.get("id")) == model:
            return True
    return False


def available_model_groups(workspace_root: Path) -> list[dict[str, Any]]:
    """
    Provider groups the UI can offer. Primary provider (from Settings) + always Ollama for local overrides.
    """
    pairs = load_effective_pairs(workspace_root)
    current = pairs.get("LLM_PROVIDER", "ollama").strip().lower()
    out: list[dict[str, Any]] = []

    # Primary configured provider first
    entry = _catalog_entry(current)
    if entry and provider_is_configured(workspace_root, current, pairs):
        models = list(entry.get("models") or [])
        if current == "openai_compatible":
            models = []  # free-text
        out.append(
            {
                "providerId": current,
                "label": entry.get("name", current),
                "configured": True,
                "isPrimary": True,
                "models": models,
                "allowCustomModelId": current == "openai_compatible",
            }
        )
    elif entry:
        out.append(
            {
                "providerId": current,
                "label": entry.get("name", current),
                "configured": False,
                "isPrimary": True,
                "models": list(entry.get("models") or []),
                "allowCustomModelId": current == "openai_compatible",
                "hint": "Complete LLM authentication in Settings to use these models.",
            }
        )

    # Ollama — optional local routing per role
    ollama_entry = _catalog_entry("ollama")
    if ollama_entry:
        out.append(
            {
                "providerId": "ollama",
                "label": ollama_entry.get("name", "Ollama (local)"),
                "configured": True,
                "isPrimary": False,
                "models": list(ollama_entry.get("models") or []),
                "allowCustomModelId": True,
                "hint": "Uses your local Ollama instance (LLM_BASE_URL / default :11434).",
            }
        )

    return out


def _allowed_model_ids(groups: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """(provider_id, model_id) pairs allowed when saving."""
    allowed: set[tuple[str, str]] = set()
    for g in groups:
        pid = g["providerId"]
        if g.get("allowCustomModelId"):
            continue
        for m in g.get("models") or []:
            mid = m.get("id")
            if mid:
                allowed.add((pid, str(mid)))
    return allowed


def load_agent_models(workspace_root: Path) -> dict[str, Any]:
    path = agent_models_path(workspace_root)
    if not path.is_file():
        return {"version": 1, "roles": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": 1, "roles": {}}
        data.setdefault("version", 1)
        data.setdefault("roles", {})
        if not isinstance(data["roles"], dict):
            data["roles"] = {}
        return data
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "roles": {}}


def save_agent_models(workspace_root: Path, data: dict[str, Any]) -> None:
    path = agent_models_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def get_agent_models_response(workspace_root: Path) -> dict[str, Any]:
    pairs = load_effective_pairs(workspace_root)
    default_model = pairs.get("LLM_MODEL", "llama3.2").strip() or "llama3.2"
    default_host = pairs.get("EXECUTION_HOST", "localhost").strip() or "localhost"
    stored = load_agent_models(workspace_root)
    groups = available_model_groups(workspace_root)
    cred_groups = available_credentials_for_agents(workspace_root)

    effective_roles: dict[str, Any] = {}
    for role in AGENT_ROLE_IDS:
        rdef = stored["roles"].get(role)
        if isinstance(rdef, str):
            rdef = {
                "provider": pairs.get("LLM_PROVIDER", "ollama"),
                "model": rdef,
                "executionHost": None,
                "credentialId": lcs.SETTINGS_DEFAULT_CREDENTIAL_ID,
            }
        elif not isinstance(rdef, dict):
            rdef = {}
        cred_id = str(rdef.get("credentialId") or rdef.get("credential_id") or "").strip()
        if not cred_id:
            cred_id = lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
        if cred_id == lcs.SETTINGS_DEFAULT_CREDENTIAL_ID:
            prov = (rdef.get("provider") or pairs.get("LLM_PROVIDER", "ollama")).strip().lower()
        elif cred_id == lcs.OLLAMA_CREDENTIAL_ID:
            prov = "ollama"
        else:
            c = lcs.get_by_id(workspace_root, cred_id)
            prov = (
                str(c.get("provider") or rdef.get("provider") or pairs.get("LLM_PROVIDER", "ollama")).strip().lower()
                if c
                else (rdef.get("provider") or pairs.get("LLM_PROVIDER", "ollama")).strip().lower()
            )
        model = (rdef.get("model") or default_model).strip() or default_model
        model = resolve_cloud_agent_model(prov, model, default_model)
        host = (rdef.get("executionHost") or default_host).strip() or default_host
        effective_roles[role] = {
            "credentialId": cred_id,
            "provider": prov,
            "model": model,
            "executionHost": host,
        }

    return {
        "defaultLlmModel": default_model,
        "defaultExecutionHost": default_host,
        "settingsProvider": pairs.get("LLM_PROVIDER", "ollama").strip().lower(),
        "roles": stored.get("roles", {}),
        "effective": effective_roles,
        "availableProviders": groups,
        "availableCredentials": cred_groups,
        "roleIds": list(AGENT_ROLE_IDS),
    }


def validate_save_agent_models(
    workspace_root: Path, payload: dict[str, Any]
) -> tuple[bool, str, dict[str, Any]]:
    groups = available_model_groups(workspace_root)
    cred_groups = available_credentials_for_agents(workspace_root)
    allowed_pairs = _allowed_model_ids(groups)
    allow_custom_providers = {g["providerId"] for g in groups if g.get("allowCustomModelId")}

    raw_roles = payload.get("roles")
    if raw_roles is not None and not isinstance(raw_roles, dict):
        return False, "roles must be an object", {}
    if raw_roles is None:
        raw_roles = {}

    pairs = load_effective_pairs(workspace_root)
    default_model = pairs.get("LLM_MODEL", "llama3.2").strip() or "llama3.2"
    default_host = pairs.get("EXECUTION_HOST", "localhost").strip() or "localhost"

    stored = load_agent_models(workspace_root)
    out_roles: dict[str, Any] = {}
    if isinstance(stored.get("roles"), dict):
        out_roles = dict(stored["roles"])

    for role, spec in raw_roles.items():
        if role not in AGENT_ROLE_IDS:
            continue
        if spec is None:
            out_roles.pop(role, None)
            continue
        if isinstance(spec, str):
            spec = {
                "provider": pairs.get("LLM_PROVIDER", "ollama"),
                "model": spec.strip(),
                "credentialId": lcs.SETTINGS_DEFAULT_CREDENTIAL_ID,
            }
        if not isinstance(spec, dict):
            return False, f"Invalid spec for role {role}", {}
        cred_id = str(spec.get("credentialId") or spec.get("credential_id") or "").strip()
        if not cred_id:
            cred_id = lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
        cg = _credential_group_by_id(cred_groups, cred_id)
        if not cg:
            return False, f"Unknown credential for role '{role}'", {}
        prov = str(cg.get("providerId") or "").strip().lower()
        model = (spec.get("model") or "").strip()
        host = (spec.get("executionHost") or "").strip() or None
        if not model:
            model = default_model
        if not provider_is_configured(workspace_root, prov, pairs, cred_id):
            return False, f"Provider '{prov}' is not configured for role '{role}'", {}
        if _role_model_allowed(cred_groups, cred_id, model):
            pass
        elif prov in allow_custom_providers and _custom_model_ok(prov, model):
            pass
        elif (prov, model) in allowed_pairs:
            pass
        else:
            return False, f"Model '{model}' is not allowed for credential '{cred_id}' on role '{role}'", {}
        entry: dict[str, Any] = {
            "credentialId": cred_id,
            "provider": prov,
            "model": model,
        }
        if host:
            entry["executionHost"] = host
        elif spec.get("executionHost") == "":
            entry["executionHost"] = default_host
        out_roles[role] = entry

    data = {"version": 1, "roles": out_roles}
    return True, "", data
