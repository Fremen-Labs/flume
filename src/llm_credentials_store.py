# Labeled LLM API keys (multi-provider).
# Metadata stored in ES index 'flume-llm-credentials'.
# API keys stored exclusively in OpenBao KV at secret/data/flume/llm_credentials/{id}.
# AP-14: Local JSON fallback removed — ES is the sole metadata store.

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

logger = get_logger("llm_credentials_store")

OLLAMA_CREDENTIAL_ID = "__ollama__"
# Use global LLM_* from env / Settings (no row in llm_credentials.json).
SETTINGS_DEFAULT_CREDENTIAL_ID = "__settings_default__"
# OpenAI ChatGPT/Codex OAuth only (uses OPENAI_OAUTH_* from env — no row in llm_credentials.json).
OPENAI_OAUTH_CREDENTIAL_ID = "__openai_oauth__"

# Map mis-tagged rows in llm_credentials.json to catalog / LLM client ids.
_PROVIDER_ALIASES: dict[str, str] = {
    "google": "gemini",
    "google-ai": "gemini",
    "google_ai": "gemini",
    "googleaistudio": "gemini",
    "generativelanguage": "gemini",
}


def normalize_provider_id(pid: str) -> str:
    p = (pid or "").strip().lower()
    return _PROVIDER_ALIASES.get(p, p)


def resolve_credential_label(workspace_root: Path, cred_id: str) -> str:
    """Short display name for UIs (worker snapshot, dashboards). No secrets."""
    cid = (cred_id or "").strip()
    if not cid or cid == SETTINGS_DEFAULT_CREDENTIAL_ID:
        return "Settings (default)"
    if cid == OLLAMA_CREDENTIAL_ID:
        return "Ollama"
    if cid == OPENAI_OAUTH_CREDENTIAL_ID:
        return "OpenAI OAuth"
    row = get_by_id(workspace_root, cid)
    if row:
        return str(row.get("label") or cid).strip() or cid
    return cid


def credentials_path(workspace_root: Path) -> Path:
    # AP-14: Retained for legacy call-site compatibility only — no longer read/written.
    return workspace_root / "worker-manager" / "llm_credentials.json"


def _default_doc() -> dict[str, Any]:
    return {"version": 1, "activeCredentialId": "", "defaultCredentialId": "", "credentials": []}


def load_document(workspace_root=None) -> dict[str, Any]:
    """Load metadata from ES. Returns empty default doc if ES is unavailable.

    AP-14: The workspace_root parameter is retained for call-site compatibility
    but is intentionally unused — all credential metadata lives in Elasticsearch.
    """
    try:
        from es_credential_store import load_llm_credentials  # type: ignore
        doc = load_llm_credentials(_default_doc)
        if doc and (doc.get("credentials") or doc.get("activeCredentialId") or doc.get("defaultCredentialId")):
            doc.setdefault("version", 1)
            if not str(doc.get("defaultCredentialId") or "").strip() and str(doc.get("activeCredentialId") or "").strip():
                doc["defaultCredentialId"] = str(doc.get("activeCredentialId") or "").strip()
            if not isinstance(doc.get("credentials"), list):
                doc["credentials"] = []
            return doc
    except Exception as e:
        logger.warning("Failed to load LLM credentials from ES — using defaults", extra={"structured_data": {"error": str(e)}})
    return _default_doc()


def save_document(workspace_root=None, doc: dict[str, Any] = None) -> None:
    """Persist metadata to ES. Secret API keys stay in OpenBao.

    AP-14: Local JSON backup removed — ES is the sole metadata store.
    workspace_root is retained for call-site back-compat but intentionally unused.
    """
    if doc is None:
        doc = _default_doc()
    # Mask secrets before persistence
    masked_doc = json.loads(json.dumps(doc))
    for cred in masked_doc.get("credentials", []):
        if cred.get("apiKey") and cred["apiKey"] not in ("", "***OPENBAO_DELEGATED***"):
            cred["apiKey"] = "***OPENBAO_DELEGATED***"
    try:
        from es_credential_store import save_llm_credentials  # type: ignore
        save_llm_credentials(masked_doc)
    except Exception as e:
        logger.error("Failed to persist LLM credentials to ES", extra={"structured_data": {"error": str(e)}})



def _key_suffix(key: str) -> str:
    t = (key or "").strip()
    if not t:
        return ""
    if len(t) <= 4:
        return "••••"
    return t[-4:]


def list_public_credentials(workspace_root: Path) -> list[dict[str, Any]]:
    """Safe for JSON API: no raw keys."""
    doc = load_document(workspace_root)
    out: list[dict[str, Any]] = []
    for c in doc.get("credentials") or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if not cid:
            continue
        key = str(c.get("apiKey") or "").strip()
        prov_raw = str(c.get("provider") or "").strip().lower()
        out.append(
            {
                "id": cid,
                "label": str(c.get("label") or cid).strip() or cid,
                "provider": normalize_provider_id(prov_raw),
                "keySuffix": _key_suffix(key),
                "hasKey": bool(key),
                "baseUrl": str(c.get("baseUrl") or "").strip(),
            }
        )
    return out


def get_by_id(workspace_root: Path, cred_id: str) -> Optional[dict[str, Any]]:
    cid = (cred_id or "").strip()
    if not cid or cid == OLLAMA_CREDENTIAL_ID or cid == OPENAI_OAUTH_CREDENTIAL_ID:
        return None
    for c in load_document(workspace_root).get("credentials") or []:
        if isinstance(c, dict) and str(c.get("id") or "").strip() == cid:
            return dict(c)
    return None


def get_resolved_for_worker(workspace_root: Path, cred_id: str) -> Optional[dict[str, str]]:
    """
    Return overrides for one LLM call: provider, api_key, base_url.
    None = use process env only (unknown / empty id / __settings_default__).
    """
    cid = (cred_id or "").strip()
    if not cid or cid == SETTINGS_DEFAULT_CREDENTIAL_ID:
        return None
    if cid == OLLAMA_CREDENTIAL_ID:
        return {
            "provider": "ollama",
            "api_key": "",
            "base_url": "",
        }
    if cid == OPENAI_OAUTH_CREDENTIAL_ID:
        # Empty api_key lets llm_client use OPENAI_OAUTH_STATE_FILE from env.
        return {
            "provider": "openai",
            "api_key": "",
            "base_url": "",
        }
    c = get_by_id(workspace_root, cid)
    if not c:
        return None
    key = str(c.get("apiKey") or "").strip()
    prov = normalize_provider_id(str(c.get("provider") or "openai").strip().lower())
    base = str(c.get("baseUrl") or "").strip()
    if not key:
        # OpenAI OAuth-only rows (or env OAuth) — empty api_key lets llm_client refresh from state file.
        if prov == "openai" and (os.environ.get("OPENAI_OAUTH_STATE_FILE") or "").strip():
            return {"provider": "openai", "api_key": "", "base_url": base}
        return None
    if key == "***OPENBAO_DELEGATED***":
        try:
            from llm_settings import _openbao_get_all  # type: ignore
            bao_vals = _openbao_get_all(workspace_root)
            delegated_key = str(bao_vals.get(f"FLUME_CRED_{cid}") or "").strip()
            if delegated_key:
                key = delegated_key
        except ImportError:
            logger.debug("OpenBao delegation import unavailable for credential resolution")
    return {"provider": prov, "api_key": key, "base_url": base}


def duplicate_label_for_provider(
    workspace_root: Path,
    provider: str,
    label: str,
    exclude_cred_id: Optional[str] = None,
) -> bool:
    """True if another credential (same provider) already uses this label (case-insensitive)."""
    pl = (provider or "").strip().lower()
    ll = (label or "").strip().lower()
    if not pl or not ll:
        return False
    ex = (exclude_cred_id or "").strip() or None
    for c in load_document(workspace_root).get("credentials") or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if ex and cid == ex:
            continue
        if str(c.get("provider") or "").strip().lower() != pl:
            continue
        if str(c.get("label") or "").strip().lower() == ll:
            return True
    return False


def upsert_credential(
    workspace_root: Path,
    cred_id: Optional[str],
    label: str,
    provider: str,
    api_key: str,
    base_url: str,
) -> str:
    """Insert or replace by id. Preserves existing apiKey when api_key is empty."""
    doc = load_document(workspace_root)
    creds: list[dict[str, Any]] = []
    for c in doc.get("credentials") or []:
        if isinstance(c, dict) and c.get("id"):
            creds.append(dict(c))
    pid = (provider or "").strip().lower()
    label = (label or "").strip() or f"{pid} key"
    key = (api_key or "").strip()
    base = (base_url or "").strip()
    new_id = (cred_id or "").strip() or uuid.uuid4().hex[:12]
    if duplicate_label_for_provider(workspace_root, pid, label, new_id):
        raise ValueError(
            f'Another saved key for provider "{pid}" is already labeled "{label}". '
            "Use a unique label per provider."
        )
    row = {"id": new_id, "label": label, "provider": pid, "apiKey": "", "baseUrl": base}
    replaced = False
    for i, c in enumerate(creds):
        if str(c.get("id")) == new_id:
            old_key = str(c.get("apiKey") or "").strip()
            row["apiKey"] = key if key else old_key
            creds[i] = row
            replaced = True
            break
    if not replaced:
        row["apiKey"] = key
        creds.append(row)
    doc["credentials"] = creds
    if key and key != "***OPENBAO_DELEGATED***":
        try:
            from llm_settings import _openbao_enabled  # type: ignore
            enabled, pairs = _openbao_enabled(workspace_root)
            if enabled:
                import urllib.request
                import json
                addr = pairs["OPENBAO_ADDR"].rstrip("/")
                token = pairs["OPENBAO_TOKEN"]
                # Use standard mount 'secret' as fallback
                mount = str(pairs.get("OPENBAO_MOUNT", "secret") or "secret").strip().strip("/")
                secret_url = f"{addr}/v1/{mount}/data/flume/llm_credentials/{new_id}"
                
                payload = json.dumps({"data": {"api_key": key}}).encode("utf-8")
                req = urllib.request.Request(
                    secret_url,
                    data=payload,
                    headers={"X-Vault-Token": token, "Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    if r.status not in (200, 201, 204):
                        logger.error(f"Failed to write credential to OpenBao: HTTP {r.status}")
        except Exception as e:
            logger.error(f"OpenBao credential upsert failed: {e}")
    save_document(workspace_root, doc)
    return new_id


def update_credential_meta(
    workspace_root: Path, cred_id: str, *, label: Optional[str] = None, base_url: Optional[str] = None
) -> bool:
    doc = load_document(workspace_root)
    creds = doc.get("credentials") or []
    found = False
    for c in creds:
        if not isinstance(c, dict):
            continue
        if str(c.get("id") or "") != cred_id:
            continue
        found = True
        if label is not None:
            nl = str(label).strip() or str(c.get("label") or "")
            prov = str(c.get("provider") or "").strip().lower()
            if duplicate_label_for_provider(workspace_root, prov, nl, cred_id):
                raise ValueError(
                    f'Another saved key for provider "{prov}" is already labeled "{nl}". '
                    "Use a unique label per provider."
                )
            c["label"] = nl
        if base_url is not None:
            c["baseUrl"] = str(base_url).strip()
        break
    if not found:
        return False
    save_document(workspace_root, doc)
    return True


def delete_credential(workspace_root: Path, cred_id: str) -> bool:
    doc = load_document(workspace_root)
    old = list(doc.get("credentials") or [])
    creds = [c for c in old if isinstance(c, dict) and str(c.get("id")) != cred_id]
    if len(creds) == len(old):
        return False
    doc["credentials"] = creds
    if str(doc.get("activeCredentialId") or "") == cred_id:
        doc["activeCredentialId"] = ""
    if str(doc.get("defaultCredentialId") or "") == cred_id:
        doc["defaultCredentialId"] = ""
    try:
        from llm_settings import _openbao_put_many  # type: ignore
        _openbao_put_many(workspace_root, {f"FLUME_CRED_{cred_id}": ""})
    except ImportError:
        logger.debug("OpenBao delegation import unavailable during credential delete")
    save_document(workspace_root, doc)
    return True


def set_active_credential_id(workspace_root: Path, cred_id: str) -> None:
    """Persist the default saved key (syncs legacy activeCredentialId for older readers)."""
    doc = load_document(workspace_root)
    cid = (cred_id or "").strip()
    doc["defaultCredentialId"] = cid
    doc["activeCredentialId"] = cid
    save_document(workspace_root, doc)


def get_active_credential_id(workspace_root: Path) -> str:
    """Default saved credential id (used for LLM_* sync and agent Settings-default fallback)."""
    doc = load_document(workspace_root)
    return str(doc.get("defaultCredentialId") or doc.get("activeCredentialId") or "").strip()


def build_activation_env_updates(workspace_root: Path, cred_id: str) -> dict[str, str]:
    """
    LLM_* env updates when user activates a saved credential (or Ollama).
    """
    from llm_settings import load_effective_pairs  # type: ignore

    pairs = load_effective_pairs(workspace_root)
    cid = (cred_id or "").strip()
    if cid == OLLAMA_CREDENTIAL_ID:
        from llm_settings import resolve_effective_ollama_base_url  # type: ignore
        base = resolve_effective_ollama_base_url(pairs)
        return {
            "LLM_PROVIDER": "ollama",
            "LLM_API_KEY": "",
            "LLM_BASE_URL": base,
            "OPENAI_OAUTH_STATE_FILE": "",
        }
    c = get_by_id(workspace_root, cid)
    if not c:
        raise ValueError(f"Unknown credential: {cred_id}")
    key = str(c.get("apiKey") or "").strip()
    if not key:
        raise ValueError("Credential has no API key")
    if key == "***OPENBAO_DELEGATED***":
        try:
            from llm_settings import _openbao_get_all  # type: ignore
            bao_vals = _openbao_get_all(workspace_root)
            delegated_key = str(bao_vals.get(f"FLUME_CRED_{cid}") or "").strip()
            if delegated_key:
                key = delegated_key
        except ImportError:
            logger.debug("OpenBao delegation import unavailable during activation")
    prov = normalize_provider_id(str(c.get("provider") or "openai").strip().lower())
    base = str(c.get("baseUrl") or "").strip()
    out: dict[str, str] = {
        "LLM_PROVIDER": prov,
        "LLM_API_KEY": key,
        "OPENAI_OAUTH_STATE_FILE": "",
    }
    if prov in ("openai", "anthropic", "gemini", "xai", "mistral", "cohere") and not base:
        out["LLM_BASE_URL"] = ""
    else:
        out["LLM_BASE_URL"] = base
    return out


def apply_credentials_action(
    workspace_root: Path, payload: dict[str, Any]
) -> tuple[bool, str, Optional[dict[str, str]]]:
    """
    Handle credential CRUD from API. Returns (ok, error, env_updates_or_none).
    env_updates are merged into .env/OpenBao when activating (set default) or saving Settings with a new key.
    """
    action = str(payload.get("action") or "").strip().lower()
    if action == "delete":
        cid = str(payload.get("id") or "").strip()
        if not cid or cid == OLLAMA_CREDENTIAL_ID:
            return False, "id is required", None
        if not delete_credential(workspace_root, cid):
            return False, "Credential not found", None
        return True, "", None

    if action in ("activate", "default"):
        cid = str(payload.get("id") or "").strip()
        if not cid:
            return False, "id is required", None
        try:
            updates = build_activation_env_updates(workspace_root, cid)
        except ValueError as e:
            return False, str(e), None
        set_active_credential_id(workspace_root, cid)
        return True, "", updates

    if action == "patch":
        cid = str(payload.get("id") or "").strip()
        if not cid or cid == OLLAMA_CREDENTIAL_ID:
            return False, "id is required", None
        label = payload.get("label")
        base_url = payload.get("baseUrl")
        if label is None and base_url is None:
            return False, "label or baseUrl required", None
        try:
            ok = update_credential_meta(
                workspace_root,
                cid,
                label=None if label is None else str(label),
                base_url=None if base_url is None else str(base_url),
            )
        except ValueError as e:
            return False, str(e), None
        if not ok:
            return False, "Credential not found", None
        return True, "", None

    if action == "upsert":
        from llm_settings import VALID_PROVIDERS  # type: ignore

        label = str(payload.get("label") or "").strip()
        provider = str(payload.get("provider") or "").strip().lower()
        if not provider:
            return False, "provider is required", None
        if provider not in VALID_PROVIDERS:
            return False, f"Invalid provider: {provider}", None
        api_key = str(payload.get("apiKey") or "").strip()
        cred_id = str(payload.get("id") or "").strip() or None
        base_url = str(payload.get("baseUrl") or "").strip()
        if not label:
            label = f"{provider} key"
        if (not api_key or api_key == "***") and not cred_id:
            return False, "apiKey is required for new credentials", None
        if (not api_key or api_key == "***") and cred_id:
            old = get_by_id(workspace_root, cred_id)
            if not old or not str(old.get("apiKey") or "").strip():
                return False, "apiKey is required", None
        if api_key == "***":
            api_key = ""
        try:
            new_id = upsert_credential(workspace_root, cred_id, label, provider, api_key, base_url)
        except ValueError as e:
            return False, str(e), None
        # Do not auto-set default key — user chooses "Set as default" or saves from Settings.
        return True, new_id, None

    return False, "action must be upsert, delete, activate, default, or patch", None
