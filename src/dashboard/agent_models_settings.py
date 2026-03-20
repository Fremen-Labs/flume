# Per-role agent model + execution host — persisted beside worker state.
# Each role can pick a saved LLM credential (labeled API key + provider) and a model.

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import llm_credentials_store as lcs
from llm_settings import PROVIDER_CATALOG, load_effective_pairs, get_oauth_status
from workspace_llm_env import normalize_gemini_model_id, resolve_cloud_agent_model

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

    if cid == lcs.OPENAI_OAUTH_CREDENTIAL_ID:
        return pid == "openai" and _provider_is_configured_env(workspace_root, "openai", pairs)

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
            if key:
                return True
            return _provider_is_configured_env(workspace_root, "openai", pairs)
        if pid == "ollama":
            return True
        return bool(key)

    return _provider_is_configured_env(workspace_root, pid, pairs)


def _oauth_configured(workspace_root: Path) -> bool:
    try:
        return bool(get_oauth_status(workspace_root).get("configured"))
    except Exception:
        return False


def available_credentials_for_agents(workspace_root: Path) -> list[dict[str, Any]]:
    """
    Credentials for the Agents UI: Settings default (current LLM_* profile), saved keys (incl. incomplete),
    optional OpenAI OAuth profile, and Ollama when not already the active Settings provider.
    """
    pairs = load_effective_pairs(workspace_root)
    out: list[dict[str, Any]] = []

    current = pairs.get("LLM_PROVIDER", "ollama").strip().lower()
    entry = _catalog_entry(current)
    if entry:
        models = list(entry.get("models") or [])
        if current == "openai_compatible":
            models = []
        settings_ok = _provider_is_configured_env(workspace_root, current, pairs)
        out.append(
            {
                "credentialId": lcs.SETTINGS_DEFAULT_CREDENTIAL_ID,
                "label": f"Settings default — {entry.get('name', current)}",
                "shortLabel": "Settings (default)",
                "providerId": current,
                "configured": settings_ok,
                "models": models,
                "allowCustomModelId": current in ("ollama", "openai_compatible"),
                "hint": None
                if settings_ok
                else "Set this provider and API key (or OAuth) under Settings → LLM.",
            }
        )
    else:
        settings_ok = _provider_is_configured_env(workspace_root, current, pairs)
        out.append(
            {
                "credentialId": lcs.SETTINGS_DEFAULT_CREDENTIAL_ID,
                "label": f"Settings default — {current}",
                "shortLabel": "Settings (default)",
                "providerId": current,
                "configured": settings_ok,
                "models": [],
                "allowCustomModelId": True,
                "hint": None if settings_ok else "Set this provider under Settings → LLM.",
            }
        )

    oauth_ok = _oauth_configured(workspace_root)
    doc = lcs.load_document(workspace_root)
    for c in doc.get("credentials") or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if not cid:
            continue
        pid = str(c.get("provider") or "").strip().lower()
        cat = _catalog_entry(pid)
        if not cat and pid != "openai_compatible":
            continue
        key = str(c.get("apiKey") or "").strip()
        base = str(c.get("baseUrl") or "").strip()
        if pid == "openai_compatible" and not base:
            continue
        models = list(cat.get("models") or []) if cat else []
        if pid == "openai_compatible":
            models = []
        disp = cat.get("name", pid) if cat else pid
        lbl = str(c.get("label") or cid).strip() or cid
        if pid == "openai":
            row_ok = bool(key) or oauth_ok
        elif pid == "openai_compatible":
            row_ok = bool(key) and bool(base)
        else:
            row_ok = bool(key)
        hint = None
        if not row_ok:
            if pid == "openai":
                hint = "Paste an API key in Settings → LLM, or configure OpenAI OAuth."
            elif pid == "openai_compatible":
                hint = "Set base URL and API key under Settings → LLM for this profile."
            else:
                hint = "Paste an API key for this label under Settings → LLM."
        ks = key[-4:] if len(key) > 4 else ("••••" if key else "")
        out.append(
            {
                "credentialId": cid,
                "label": f"{lbl} · {disp}",
                "shortLabel": lbl,
                "providerId": pid,
                "configured": row_ok,
                "keySuffix": ks,
                "models": models,
                "allowCustomModelId": pid in ("ollama", "openai_compatible"),
                "hint": hint,
            }
        )

    # Explicit OpenAI OAuth option when Codex/ChatGPT login is set up (even if Settings uses API key).
    openai_entry = _catalog_entry("openai")
    if openai_entry and oauth_ok:
        out.append(
            {
                "credentialId": lcs.OPENAI_OAUTH_CREDENTIAL_ID,
                "label": "OpenAI (OAuth — ChatGPT / Codex)",
                "shortLabel": "OpenAI OAuth",
                "providerId": "openai",
                "configured": True,
                "models": list(openai_entry.get("models") or []),
                "allowCustomModelId": False,
                "hint": "Uses OPENAI_OAUTH_STATE_FILE from Settings / .env (not the platform API key).",
            }
        )

    ollama_entry = _catalog_entry("ollama")
    if ollama_entry and current != "ollama":
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
    valid_cred_ids = {str(g.get("credentialId") or "") for g in cred_groups if g.get("credentialId")}

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
        if cred_id not in valid_cred_ids:
            cred_id = lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
        if cred_id == lcs.SETTINGS_DEFAULT_CREDENTIAL_ID:
            prov = (rdef.get("provider") or pairs.get("LLM_PROVIDER", "ollama")).strip().lower()
        elif cred_id == lcs.OLLAMA_CREDENTIAL_ID:
            prov = "ollama"
        elif cred_id == lcs.OPENAI_OAUTH_CREDENTIAL_ID:
            prov = "openai"
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
            cred_id = lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
            cg = _credential_group_by_id(cred_groups, cred_id)
        if not cg:
            return False, f"Unknown credential for role '{role}'", {}
        if not cg.get("configured"):
            return (
                False,
                f"Credential '{cg.get('shortLabel') or cred_id}' is not ready — add its API key in Settings → LLM (role '{role}').",
                {},
            )
        prov = str(cg.get("providerId") or "").strip().lower()
        model = (spec.get("model") or "").strip()
        host = (spec.get("executionHost") or "").strip() or None
        if not model:
            model = default_model
        if prov == "gemini":
            model = normalize_gemini_model_id(model)
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
