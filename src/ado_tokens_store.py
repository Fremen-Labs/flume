# Labeled Azure DevOps credentials (PAT + org URL per row).
# Metadata stored in ES index 'flume-ado-tokens'.
# PATs stored exclusively in OpenBao KV at secret/data/flume/ado_tokens/{id}.
# AP-14: Local JSON fallback removed — ES is the sole metadata store.

from __future__ import annotations

import json
import uuid
from typing import Any, Optional, Literal
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from utils.logger import get_logger

logger = get_logger("ado_tokens_store")

MASK = "***"
OPENBAO_DELEGATED_MASK = "***OPENBAO_DELEGATED***"
ENV_ADO_TOKEN = "ADO_TOKEN"
ENV_ADO_ORG_URL = "ADO_ORG_URL"
DEFAULT_LABEL = "Azure DevOps"
DEFAULT_LEGACY_LABEL = "Default"

ACTION_DELETE = "delete"
ACTION_SETACTIVE = "setactive"
ACTION_UPSERT = "upsert"


class AdoCredential(BaseModel):
    id: str
    label: str = DEFAULT_LABEL
    token: str = ""
    orgUrl: str = ""


class AdoMetadataDoc(BaseModel):
    version: int = 1
    activeCredentialId: str = ""
    credentials: list[AdoCredential] = Field(default_factory=list)


class AdoActionPayload(BaseModel):
    action: Literal["upsert", "delete", "setactive"]
    id: Optional[str] = None
    label: Optional[str] = None
    token: Optional[str] = None
    orgUrl: Optional[str] = None


def _default_doc() -> dict[str, Any]:
    return AdoMetadataDoc().model_dump()


def load_document(workspace_root: Any = None) -> dict[str, Any]:
    """Load metadata from ES. Returns empty default doc if ES is unavailable.

    AP-14: The workspace_root parameter is retained for call-site compatibility
    but is intentionally unused — all credential metadata lives in Elasticsearch.
    """
    try:
        from es_credential_store import load_ado_tokens
        doc = load_ado_tokens(_default_doc)
        if doc and (doc.get("credentials") or doc.get("activeCredentialId")):
            return AdoMetadataDoc(**doc).model_dump()
    except Exception as e:
        logger.warning("Failed to load ADO tokens from ES — using defaults", extra={"structured_data": {"error": str(e)}})
    return _default_doc()


def save_document(workspace_root: Path, doc: dict[str, Any]) -> None:
    """Persist metadata to ES. Secrets (PAT) stay in OpenBao."""
    try:
        model = AdoMetadataDoc(**doc)
    except ValidationError as e:
        logger.error("Invalid ADO document structure during save", extra={"structured_data": {"error": str(e)}})
        return

    # Mask the token before saving
    masked_model = model.model_copy(deep=True)
    for cred in masked_model.credentials:
        if cred.token and cred.token not in (MASK, OPENBAO_DELEGATED_MASK):
            cred.token = OPENBAO_DELEGATED_MASK

    try:
        from es_credential_store import save_ado_tokens
        # Use model_dump(mode="json") to ensure it serialize perfectly to dict
        save_ado_tokens(json.loads(masked_model.model_dump_json()))
    except Exception as e:
        logger.error("Failed to persist ADO tokens to ES", extra={"structured_data": {"error": str(e)}})


def _strip_env_quotes(raw: str) -> str:
    t = (raw or "").strip()
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        return t[1:-1].strip()
    return t


def _token_suffix(token: str) -> str:
    t = (token or "").strip()
    if not t:
        return ""
    if len(t) <= 4:
        return "••••"
    return t[-4:]


def ensure_migrated_from_env(workspace_root: Path) -> None:
    """Import env ADO_TOKEN / ADO_ORG_URL into the store when the file is empty."""
    doc = AdoMetadataDoc(**load_document(workspace_root))
    if doc.credentials:
        return
    try:
        from llm_settings import load_effective_pairs  # type: ignore
    except ImportError:
        return

    pairs = load_effective_pairs(workspace_root)
    raw_tok = _strip_env_quotes(pairs.get(ENV_ADO_TOKEN, "") or "")
    raw_org = str(pairs.get(ENV_ADO_ORG_URL, "") or "").strip()
    if not raw_tok and not raw_org:
        return
    
    cid = uuid.uuid4().hex[:12]
    new_cred = AdoCredential(id=cid, label=DEFAULT_LEGACY_LABEL, token=raw_tok, orgUrl=raw_org)
    doc.credentials.append(new_cred)
    doc.activeCredentialId = cid if raw_tok else ""
    save_document(workspace_root, doc.model_dump())


def _sync_active_to_env(workspace_root: Path) -> None:
    # AP-10: ADO tokens are no longer written to .env at runtime.
    # Workers read the active token directly from ES (flume-ado-tokens) + OpenBao
    # via get_active_token_plain(). This function is intentionally a no-op.
    pass


def get_active_credential_id(workspace_root: Path) -> str:
    doc = AdoMetadataDoc(**load_document(workspace_root))
    return doc.activeCredentialId.strip()


def get_active_token_plain(workspace_root: Path) -> str:
    doc = AdoMetadataDoc(**load_document(workspace_root))
    aid = doc.activeCredentialId.strip()
    for c in doc.credentials:
        if c.id.strip() != aid:
            continue
        token = c.token.strip()
        if token == OPENBAO_DELEGATED_MASK:
            try:
                from llm_settings import _openbao_get_all  # type: ignore
                bao_vals = _openbao_get_all(workspace_root)
                delegated_token = str(bao_vals.get(f"FLUME_ADO_{aid}") or "").strip()
                if delegated_token:
                    token = delegated_token
            except ImportError:
                logger.debug("OpenBao delegation import unavailable for ADO token resolution")
        return token
    return ""


def get_active_org_url(workspace_root: Path) -> str:
    doc = AdoMetadataDoc(**load_document(workspace_root))
    aid = doc.activeCredentialId.strip()
    for c in doc.credentials:
        if c.id.strip() != aid:
            continue
        return c.orgUrl.strip()
    return ""


def list_public_credentials(workspace_root: Path) -> list[dict[str, Any]]:
    doc = AdoMetadataDoc(**load_document(workspace_root))
    out: list[dict[str, Any]] = []
    for c in doc.credentials:
        cid = c.id.strip()
        if not cid:
            continue
        key = c.token.strip()
        out.append(
            {
                "id": cid,
                "label": c.label.strip() or cid,
                "orgUrl": c.orgUrl.strip(),
                "tokenSuffix": _token_suffix(key),
                "hasToken": bool(key),
            }
        )
    return out


def _label_taken(creds: list[AdoCredential], label: str, exclude_id: Optional[str]) -> bool:
    ll = (label or "").strip().lower()
    if not ll:
        return False
    ex = (exclude_id or "").strip() or None
    for c in creds:
        cid = c.id.strip()
        if ex and cid == ex:
            continue
        if c.label.strip().lower() == ll:
            return True
    return False


def _safe_bao_put(workspace_root: Path, key: str, value: str, context: str) -> None:
    try:
        from llm_settings import _openbao_put_many  # type: ignore
        _openbao_put_many(workspace_root, {key: value})
    except ImportError:
        logger.debug(f"OpenBao put unavailable during {context}")


def _handle_delete(workspace_root: Path, doc: AdoMetadataDoc, payload: AdoActionPayload) -> tuple[bool, str]:
    cid = (payload.id or "").strip()
    if not cid:
        return False, "id is required"
    
    new_creds = [c for c in doc.credentials if c.id != cid]
    if len(new_creds) == len(doc.credentials):
        return False, "ADO credential not found"
        
    doc.credentials = new_creds
    if doc.activeCredentialId == cid:
        doc.activeCredentialId = new_creds[0].id.strip() if new_creds else ""
        
    _safe_bao_put(workspace_root, f"FLUME_ADO_{cid}", "", "ADO token delete")
    
    save_document(workspace_root, doc.model_dump())
    _sync_active_to_env(workspace_root)
    return True, ""


def _handle_setactive(workspace_root: Path, doc: AdoMetadataDoc, payload: AdoActionPayload) -> tuple[bool, str]:
    cid = (payload.id or "").strip()
    if not cid:
        return False, "id is required"
        
    row = next((c for c in doc.credentials if c.id == cid), None)
    if not row:
        return False, "ADO credential not found"
    if not row.token.strip():
        return False, "PAT is empty — paste a token before setting active"
        
    doc.activeCredentialId = cid
    save_document(workspace_root, doc.model_dump())
    _sync_active_to_env(workspace_root)
    return True, ""


def _handle_upsert(workspace_root: Path, doc: AdoMetadataDoc, payload: AdoActionPayload) -> tuple[bool, str]:
    label = (payload.label or "").strip()
    cred_id = (payload.id or "").strip() or None
    token_in = (payload.token or "").strip() if payload.token is not None else ""
    org_in = (payload.orgUrl or "").strip() if payload.orgUrl is not None else ""
    
    has_token_key = payload.token is not None
    has_org_key = payload.orgUrl is not None

    if cred_id:
        row = next((c for c in doc.credentials if c.id == cred_id), None)
        if not row:
            return False, "ADO credential not found"
        if label:
            if _label_taken(doc.credentials, label, cred_id):
                return False, f'Another credential is already labeled "{label}"'
            row.label = label
        if has_token_key and token_in != MASK:
            row.token = token_in
        if has_org_key:
            row.orgUrl = org_in
            
        if not doc.activeCredentialId.strip() and row.token.strip():
            doc.activeCredentialId = cred_id
            
        if has_token_key and token_in and token_in != MASK:
            _safe_bao_put(workspace_root, f"FLUME_ADO_{cred_id}", token_in, "ADO token upsert (update)")
            
        save_document(workspace_root, doc.model_dump())
        _sync_active_to_env(workspace_root)
        return True, ""

    # New credential
    if not label:
        label = DEFAULT_LABEL
    if _label_taken(doc.credentials, label, None):
        return False, f'Another credential is already labeled "{label}"'
    if not token_in or token_in == MASK:
        return False, "PAT is required for new ADO credentials"
    if not org_in:
        return False, "Organization URL is required when adding ADO credentials (pair with PAT)"
        
    new_id = uuid.uuid4().hex[:12]
    new_cred = AdoCredential(id=new_id, label=label, token=token_in, orgUrl=org_in)
    doc.credentials.append(new_cred)
    
    if not doc.activeCredentialId.strip():
        doc.activeCredentialId = new_id
        
    if token_in and token_in != MASK:
        _safe_bao_put(workspace_root, f"FLUME_ADO_{new_id}", token_in, "ADO token upsert (new)")
        
    save_document(workspace_root, doc.model_dump())
    _sync_active_to_env(workspace_root)
    return True, ""


def apply_action(workspace_root: Path, body: dict[str, Any]) -> tuple[bool, str]:
    try:
        payload = AdoActionPayload(**body)
    except ValidationError as e:
        return False, f"Invalid payload: {e}"

    doc = AdoMetadataDoc(**load_document(workspace_root))

    if payload.action == ACTION_DELETE:
        return _handle_delete(workspace_root, doc, payload)
    if payload.action == ACTION_SETACTIVE:
        return _handle_setactive(workspace_root, doc, payload)
    if payload.action == ACTION_UPSERT:
        return _handle_upsert(workspace_root, doc, payload)

    return False, "adoTokenAction.action must be upsert, delete, or setActive"


def apply_legacy_patch(
    workspace_root: Path,
    *,
    update_token: bool = False,
    token: str = "",
    update_org: bool = False,
    org_url: str = "",
) -> tuple[bool, str]:
    """Legacy POST: only apply fields the client included (and not sent as ***)."""
    if not update_token and not update_org:
        return True, ""
        
    ensure_migrated_from_env(workspace_root)
    doc = AdoMetadataDoc(**load_document(workspace_root))
    aid = doc.activeCredentialId.strip()

    def apply_to_row(row: AdoCredential) -> None:
        if update_token:
            row.token = token
        if update_org:
            row.orgUrl = org_url

    if aid:
        for c in doc.credentials:
            if c.id == aid:
                apply_to_row(c)
                if update_token and token and token != MASK:
                    _safe_bao_put(workspace_root, f"FLUME_ADO_{aid}", token, "ADO legacy patch (active)")
                save_document(workspace_root, doc.model_dump())
                _sync_active_to_env(workspace_root)
                return True, ""

    new_id = uuid.uuid4().hex[:12]
    new_cred = AdoCredential(id=new_id, label=DEFAULT_LEGACY_LABEL, token="", orgUrl="")
    apply_to_row(new_cred)
    doc.credentials.append(new_cred)
    doc.activeCredentialId = new_id if new_cred.token.strip() else ""
    
    if update_token and token and token != MASK:
        _safe_bao_put(workspace_root, f"FLUME_ADO_{new_id}", token, "ADO legacy patch (new)")
        
    save_document(workspace_root, doc.model_dump())
    _sync_active_to_env(workspace_root)
    return True, ""
