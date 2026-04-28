# Per-role agent model + execution host — persisted beside worker state.
# Each role can pick a saved LLM credential (labeled API key + provider) and a model.

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import BaseModel, Field, ValidationError

from utils.exceptions import SAFE_EXCEPTIONS
from utils.logger import get_logger
import llm_credentials_store as lcs  # type: ignore
from llm_settings import load_effective_pairs, get_oauth_status, provider_catalog_for_workspace  # type: ignore
from workspace_llm_env import normalize_gemini_model_id, resolve_cloud_agent_model  # type: ignore

logger = get_logger("agent_models_settings")

# ─── Constants ─────────────────────────────────────────────────────────────────

# Ordered tuple for UI display; frozenset for O(1) validation lookups.
AGENT_ROLE_IDS = (
    "intake",
    "pm",
    "implementer",
    "tester",
    "reviewer",
    "memory-updater",
    "auto-unblocker",
)
_AGENT_ROLE_ID_SET = frozenset(AGENT_ROLE_IDS)

DEFAULT_MODEL = "llama3.2"
DEFAULT_HOST = "localhost"
PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENAI = "openai"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
PROVIDER_GEMINI = "gemini"

# Providers that allow free-text model IDs (no catalog restriction).
_CUSTOM_MODEL_PROVIDERS = frozenset({PROVIDER_OLLAMA, PROVIDER_OPENAI_COMPATIBLE, "xai", "grok"})

# Frontier providers that require an API key to be considered "configured".
_API_KEY_PROVIDERS = frozenset({"anthropic", PROVIDER_GEMINI, "xai", "grok", "mistral", "cohere"})

# Model ID validation.
_MODEL_ID_PATTERN = re.compile(r"^[\w.\-:/]+$")
_MODEL_ID_MAX_LENGTH = 200


# ─── Pydantic Models ──────────────────────────────────────────────────────────


class AgentRoleSpec(BaseModel):
    """Represents the per-role configuration: credential, provider, model, and host."""

    provider: Optional[str] = None
    model: Optional[str] = None
    executionHost: Optional[str] = None
    credentialId: Optional[str] = Field(
        default=lcs.SETTINGS_DEFAULT_CREDENTIAL_ID, alias="credential_id"
    )

    model_config = {"populate_by_name": True, "extra": "ignore"}


class AgentModelsDoc(BaseModel):
    """Persistent agent_models.json structure."""

    version: int = 1
    roles: dict[str, Union[AgentRoleSpec, str]] = Field(default_factory=dict)


class AgentModelsPayload(BaseModel):
    """Incoming UI payload for saving agent model configurations."""

    roles: Optional[dict[str, Optional[Union[AgentRoleSpec, str, dict[str, Any]]]]] = None


class SettingsContext(BaseModel):
    """Unit-of-Work context that pre-loads all external state exactly once at the
    request boundary. Eliminates redundant I/O calls to ES, OpenBao, and Ollama.

    A single call to ``SettingsContext.build(workspace_root)`` replaces what was
    previously 4+ separate ``load_effective_pairs`` calls, 4+ ``_openbao_get_all``
    HTTP requests, and multiple ``lcs.load_document`` disk/ES reads per request.
    """

    pairs: dict[str, str]
    catalog_index: dict[str, dict[str, Any]]
    catalog_list: list[dict[str, Any]]
    credentials_doc: dict[str, Any]
    credentials_by_id: dict[str, dict[str, Any]]
    oauth_configured: bool
    default_model: str = DEFAULT_MODEL
    default_host: str = DEFAULT_HOST

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def build(cls, workspace_root: Path) -> SettingsContext:
        """Single factory that performs all I/O exactly once."""
        pairs = load_effective_pairs(workspace_root)
        catalog_list = provider_catalog_for_workspace(workspace_root)
        catalog_index = {p["id"]: p for p in catalog_list}
        credentials_doc = lcs.load_document(workspace_root)

        creds_by_id: dict[str, dict[str, Any]] = {}
        for c in credentials_doc.get("credentials") or []:
            if isinstance(c, dict):
                cid = str(c.get("id") or "").strip()
                if cid:
                    creds_by_id[cid] = c

        try:
            oauth_ok = bool(get_oauth_status(workspace_root).get("configured"))
        except SAFE_EXCEPTIONS:
            oauth_ok = False

        default_model = pairs.get("LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        default_host = pairs.get("EXECUTION_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST

        return cls(
            pairs=pairs,
            catalog_index=catalog_index,
            catalog_list=catalog_list,
            credentials_doc=credentials_doc,
            credentials_by_id=creds_by_id,
            oauth_configured=oauth_ok,
            default_model=default_model,
            default_host=default_host,
        )

    @property
    def current_provider(self) -> str:
        return self.pairs.get("LLM_PROVIDER", PROVIDER_OLLAMA).strip().lower()


# ─── Filesystem Persistence ───────────────────────────────────────────────────


def agent_models_path(workspace_root: Path) -> Path:
    return workspace_root / "worker-manager" / "agent_models.json"


def load_agent_models(workspace_root: Path) -> dict[str, Any]:
    path = agent_models_path(workspace_root)
    default_doc = AgentModelsDoc().model_dump()
    if not path.is_file():
        return default_doc
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_doc
        return AgentModelsDoc(**data).model_dump()
    except (OSError, json.JSONDecodeError, ValidationError):
        return default_doc


def save_agent_models(workspace_root: Path, data: dict[str, Any]) -> None:
    try:
        model = AgentModelsDoc(**data)
    except ValidationError:
        return
    path = agent_models_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model.model_dump(), indent=2) + "\n", encoding="utf-8")


# ─── Provider Configuration Checks ────────────────────────────────────────────


def _provider_is_configured_env(ctx: SettingsContext, provider_id: str) -> bool:
    """Whether LLM_* env / OpenBao supports this provider (legacy / Settings default)."""
    pid = provider_id.strip().lower()
    current = ctx.current_provider
    api_key = ctx.pairs.get("LLM_API_KEY", "").strip()
    base_url = ctx.pairs.get("LLM_BASE_URL", "").strip()

    if pid == PROVIDER_OLLAMA:
        return True

    if pid == PROVIDER_OPENAI:
        if current != PROVIDER_OPENAI:
            return False
        if api_key:
            return True
        return ctx.oauth_configured

    if pid == PROVIDER_OPENAI_COMPATIBLE:
        return current == PROVIDER_OPENAI_COMPATIBLE and bool(api_key) and bool(base_url)

    if pid in _API_KEY_PROVIDERS:
        return current == pid and bool(api_key)

    return False


def _provider_is_configured_ctx(
    ctx: SettingsContext,
    provider_id: str,
    credential_id: Optional[str] = None,
) -> bool:
    """Context-aware provider configuration check (zero additional I/O)."""
    pid = provider_id.strip().lower()
    cid = (credential_id or "").strip()

    if cid == lcs.OLLAMA_CREDENTIAL_ID:
        return pid == PROVIDER_OLLAMA

    if cid == lcs.OPENAI_OAUTH_CREDENTIAL_ID:
        return pid == PROVIDER_OPENAI and _provider_is_configured_env(ctx, PROVIDER_OPENAI)

    if cid and cid not in ("", lcs.SETTINGS_DEFAULT_CREDENTIAL_ID):
        cred = ctx.credentials_by_id.get(cid)
        if not cred:
            return False
        cprov = lcs.normalize_provider_id(str(cred.get("provider") or "").strip().lower())
        if cprov != pid:
            return False
        key = str(cred.get("apiKey") or "").strip()
        if pid == PROVIDER_OPENAI_COMPATIBLE:
            return bool(key) and bool(str(cred.get("baseUrl") or "").strip())
        if pid == PROVIDER_OPENAI:
            if key:
                return True
            return _provider_is_configured_env(ctx, PROVIDER_OPENAI)
        if pid == PROVIDER_OLLAMA:
            return True
        return bool(key)

    return _provider_is_configured_env(ctx, pid)


def provider_is_configured(
    workspace_root: Path,
    provider_id: str,
    pairs: dict[str, str],
    credential_id: Optional[str] = None,
) -> bool:
    """Public backward-compatible wrapper. Builds a lightweight context internally.

    Callers inside this module should prefer ``_provider_is_configured_ctx``.
    """
    ctx = SettingsContext.build(workspace_root)
    return _provider_is_configured_ctx(ctx, provider_id, credential_id)


# ─── Credential & Model Group Builders ─────────────────────────────────────────


def _build_credentials_for_agents(ctx: SettingsContext) -> list[dict[str, Any]]:
    """Build credential options for the Agents UI.

    Includes: Settings default (current LLM_* profile), all saved keys,
    optional OpenAI OAuth profile, and Ollama when not the active Settings provider.
    """
    out: list[dict[str, Any]] = []
    current = ctx.current_provider

    # ── Settings default ───────────────────────────────────────────────────
    entry = ctx.catalog_index.get(current)
    if entry:
        models = list(entry.get("models") or [])
        if current == PROVIDER_OPENAI_COMPATIBLE:
            models = []
        settings_ok = _provider_is_configured_env(ctx, current)
        out.append(
            {
                "credentialId": lcs.SETTINGS_DEFAULT_CREDENTIAL_ID,
                "label": f"Settings default — {entry.get('name', current)}",
                "shortLabel": "Settings (default)",
                "providerId": current,
                "configured": settings_ok,
                "models": models,
                "allowCustomModelId": current in _CUSTOM_MODEL_PROVIDERS,
                "hint": None
                if settings_ok
                else "Set this provider and API key (or OAuth) under Settings → LLM.",
            }
        )
    else:
        settings_ok = _provider_is_configured_env(ctx, current)
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

    # ── Saved credentials ──────────────────────────────────────────────────
    for c in ctx.credentials_doc.get("credentials") or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if not cid:
            continue
        pid = lcs.normalize_provider_id(str(c.get("provider") or "").strip().lower())
        cat = ctx.catalog_index.get(pid)
        if not cat and pid != PROVIDER_OPENAI_COMPATIBLE:
            continue
        key = str(c.get("apiKey") or "").strip()
        base = str(c.get("baseUrl") or "").strip()
        if pid == PROVIDER_OPENAI_COMPATIBLE and not base:
            continue
        models = list(cat.get("models") or []) if cat else []
        if pid == PROVIDER_OPENAI_COMPATIBLE:
            models = []
        disp = cat.get("name", pid) if cat else pid
        lbl = str(c.get("label") or cid).strip() or cid
        if pid == PROVIDER_OPENAI:
            row_ok = bool(key) or ctx.oauth_configured
        elif pid == PROVIDER_OPENAI_COMPATIBLE:
            row_ok = bool(key) and bool(base)
        else:
            row_ok = bool(key)
        hint = None
        if not row_ok:
            if pid == PROVIDER_OPENAI:
                hint = "Paste an API key in Settings → LLM, or configure OpenAI OAuth."
            elif pid == PROVIDER_OPENAI_COMPATIBLE:
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
                "allowCustomModelId": pid in _CUSTOM_MODEL_PROVIDERS,
                "hint": hint,
            }
        )

    # ── Explicit OpenAI OAuth ──────────────────────────────────────────────
    openai_entry = ctx.catalog_index.get(PROVIDER_OPENAI)
    if openai_entry and ctx.oauth_configured:
        out.append(
            {
                "credentialId": lcs.OPENAI_OAUTH_CREDENTIAL_ID,
                "label": "OpenAI (OAuth — ChatGPT / Codex)",
                "shortLabel": "OpenAI OAuth",
                "providerId": PROVIDER_OPENAI,
                "configured": True,
                "models": list(openai_entry.get("models") or []),
                "allowCustomModelId": False,
                "hint": "Uses ChatGPT / Codex OAuth. Workers and planner can route through Codex app-server; model selection still applies per role.",
            }
        )

    # ── Ollama fallback ────────────────────────────────────────────────────
    ollama_entry = ctx.catalog_index.get(PROVIDER_OLLAMA)
    if ollama_entry and current != PROVIDER_OLLAMA:
        out.append(
            {
                "credentialId": lcs.OLLAMA_CREDENTIAL_ID,
                "label": ollama_entry.get("name", "Ollama (local)"),
                "shortLabel": "Ollama",
                "providerId": PROVIDER_OLLAMA,
                "configured": True,
                "models": list(ollama_entry.get("models") or []),
                "allowCustomModelId": True,
                "hint": "Uses the effective Ollama endpoint from Settings or LOCAL_OLLAMA_BASE_URL.",
            }
        )

    return out


def _build_model_groups(ctx: SettingsContext) -> list[dict[str, Any]]:
    """Provider groups the UI can offer. Primary provider (from Settings) + always Ollama."""
    current = ctx.current_provider
    out: list[dict[str, Any]] = []

    entry = ctx.catalog_index.get(current)
    if entry and _provider_is_configured_ctx(ctx, current):
        models = list(entry.get("models") or [])
        if current == PROVIDER_OPENAI_COMPATIBLE:
            models = []  # free-text
        out.append(
            {
                "providerId": current,
                "label": entry.get("name", current),
                "configured": True,
                "isPrimary": True,
                "models": models,
                "allowCustomModelId": current == PROVIDER_OPENAI_COMPATIBLE,
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
                "allowCustomModelId": current == PROVIDER_OPENAI_COMPATIBLE,
                "hint": "Complete LLM authentication in Settings to use these models.",
            }
        )

    # Ollama — optional local routing per role
    ollama_entry = ctx.catalog_index.get(PROVIDER_OLLAMA)
    if ollama_entry:
        out.append(
            {
                "providerId": PROVIDER_OLLAMA,
                "label": ollama_entry.get("name", "Ollama (local)"),
                "configured": True,
                "isPrimary": False,
                "models": list(ollama_entry.get("models") or []),
                "allowCustomModelId": True,
                "hint": "Uses the effective Ollama endpoint from Settings or LOCAL_OLLAMA_BASE_URL.",
            }
        )

    return out


# ─── Public wrappers (backward compat for external callers) ────────────────────


def available_credentials_for_agents(workspace_root: Path) -> list[dict[str, Any]]:
    """Credentials for the Agents UI."""
    ctx = SettingsContext.build(workspace_root)
    return _build_credentials_for_agents(ctx)


def available_model_groups(workspace_root: Path) -> list[dict[str, Any]]:
    """Provider groups the UI can offer."""
    ctx = SettingsContext.build(workspace_root)
    return _build_model_groups(ctx)


# ─── Model Validation Helpers ──────────────────────────────────────────────────


def _credential_group_by_id(groups: list[dict[str, Any]], cred_id: str) -> Optional[dict[str, Any]]:
    for g in groups:
        if g.get("credentialId") == cred_id:
            return g
    return None


def _custom_model_ok(provider_id: str, model_id: str) -> bool:
    mid = model_id.strip()
    if not mid or len(mid) > _MODEL_ID_MAX_LENGTH:
        return False
    if not _MODEL_ID_PATTERN.match(mid):
        return False
    return provider_id in _CUSTOM_MODEL_PROVIDERS


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


# ─── Role Resolution ──────────────────────────────────────────────────────────


def _resolve_role_provider(
    ctx: SettingsContext,
    spec: AgentRoleSpec,
    cred_id: str,
) -> str:
    """Determine the effective provider for a single role, given its credential."""
    if cred_id == lcs.SETTINGS_DEFAULT_CREDENTIAL_ID:
        return (spec.provider or ctx.pairs.get("LLM_PROVIDER", PROVIDER_OLLAMA)).strip().lower()
    if cred_id == lcs.OLLAMA_CREDENTIAL_ID:
        return PROVIDER_OLLAMA
    if cred_id == lcs.OPENAI_OAUTH_CREDENTIAL_ID:
        return PROVIDER_OPENAI

    c = ctx.credentials_by_id.get(cred_id)
    if c:
        return lcs.normalize_provider_id(
            str(c.get("provider") or spec.provider or ctx.pairs.get("LLM_PROVIDER", PROVIDER_OLLAMA)).strip().lower()
        )
    return (spec.provider or ctx.pairs.get("LLM_PROVIDER", PROVIDER_OLLAMA)).strip().lower()


# ─── Entry Points ─────────────────────────────────────────────────────────────


def get_agent_models_response(workspace_root: Path) -> dict[str, Any]:
    ctx = SettingsContext.build(workspace_root)
    stored = load_agent_models(workspace_root)
    groups = _build_model_groups(ctx)
    cred_groups = _build_credentials_for_agents(ctx)
    valid_cred_ids = {str(g.get("credentialId") or "") for g in cred_groups if g.get("credentialId")}

    effective_roles: dict[str, Any] = {}
    for role in AGENT_ROLE_IDS:
        rdef = stored["roles"].get(role)
        if isinstance(rdef, str):
            rdef = {
                "provider": ctx.pairs.get("LLM_PROVIDER", PROVIDER_OLLAMA),
                "model": rdef,
                "executionHost": None,
                "credentialId": lcs.SETTINGS_DEFAULT_CREDENTIAL_ID,
            }
        elif not isinstance(rdef, dict):
            rdef = {}

        try:
            spec = AgentRoleSpec(**rdef)
        except ValidationError:
            spec = AgentRoleSpec()

        cred_id = (spec.credentialId or "").strip()
        if not cred_id:
            cred_id = lcs.SETTINGS_DEFAULT_CREDENTIAL_ID
        if cred_id not in valid_cred_ids:
            cred_id = lcs.SETTINGS_DEFAULT_CREDENTIAL_ID

        prov = _resolve_role_provider(ctx, spec, cred_id)
        model = (spec.model or ctx.default_model).strip() or ctx.default_model
        model = resolve_cloud_agent_model(prov, model, ctx.default_model)
        host = (spec.executionHost or ctx.default_host).strip() or ctx.default_host

        effective_roles[role] = {
            "credentialId": cred_id,
            "provider": prov,
            "model": model,
            "executionHost": host,
        }

    return {
        "defaultLlmModel": ctx.default_model,
        "defaultExecutionHost": ctx.default_host,
        "settingsProvider": ctx.current_provider,
        "roles": stored.get("roles", {}),
        "effective": effective_roles,
        "availableProviders": groups,
        "availableCredentials": cred_groups,
        "roleIds": list(AGENT_ROLE_IDS),
    }


def validate_save_agent_models(
    workspace_root: Path, raw_payload: dict[str, Any]
) -> tuple[bool, str, dict[str, Any]]:
    ctx = SettingsContext.build(workspace_root)
    groups = _build_model_groups(ctx)
    cred_groups = _build_credentials_for_agents(ctx)
    allowed_pairs = _allowed_model_ids(groups)
    allow_custom_providers = {g["providerId"] for g in groups if g.get("allowCustomModelId")}

    try:
        payload = AgentModelsPayload(**raw_payload)
    except ValidationError as e:
        return False, f"Invalid payload structure: {e}", {}

    raw_roles = payload.roles
    if raw_roles is None:
        raw_roles = {}

    stored = load_agent_models(workspace_root)
    out_roles: dict[str, Any] = {}
    if isinstance(stored.get("roles"), dict):
        out_roles = dict(stored["roles"])

    for role, spec in raw_roles.items():
        if role not in _AGENT_ROLE_ID_SET:
            continue
        if spec is None:
            out_roles.pop(role, None)
            continue

        if isinstance(spec, str):
            role_spec = AgentRoleSpec(
                provider=ctx.pairs.get("LLM_PROVIDER", PROVIDER_OLLAMA),
                model=spec.strip(),
                credentialId=lcs.SETTINGS_DEFAULT_CREDENTIAL_ID,
            )
        elif isinstance(spec, AgentRoleSpec):
            role_spec = spec
        else:
            try:
                role_spec = AgentRoleSpec(**spec)
            except ValidationError as e:
                return False, f"Invalid spec for role {role}: {e}", {}

        cred_id = (role_spec.credentialId or "").strip()
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
        model = (role_spec.model or "").strip()
        host = (role_spec.executionHost or "").strip() or None

        if not model:
            model = ctx.default_model
        if prov == PROVIDER_GEMINI:
            model = normalize_gemini_model_id(model)

        if not _provider_is_configured_ctx(ctx, prov, cred_id):
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
        elif isinstance(spec, dict) and spec.get("executionHost") == "":
            entry["executionHost"] = ctx.default_host
        elif isinstance(spec, AgentRoleSpec) and spec.executionHost == "":
            entry["executionHost"] = ctx.default_host

        out_roles[role] = entry

    data = {"version": 1, "roles": out_roles}
    return True, "", data
