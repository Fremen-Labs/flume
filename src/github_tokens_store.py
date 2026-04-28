# Labeled GitHub PATs (multiple).
# Metadata stored in ES index 'flume-github-tokens'.
# PATs stored exclusively in OpenBao KV at secret/data/flume/github_tokens/{id}.
# AP-14: Local JSON fallback removed — ES is the sole metadata store.

from __future__ import annotations

import re
import json
import uuid
from pathlib import Path
from typing import Any, Optional, Literal

from pydantic import BaseModel, Field, ValidationError

from utils.logger import get_logger

logger = get_logger("github_tokens_store")

MASK = "***"
OPENBAO_DELEGATED_MASK = "***OPENBAO_DELEGATED***"
ENV_GH_TOKEN = "GH_TOKEN"
DEFAULT_LABEL = "GitHub PAT"
DEFAULT_LEGACY_LABEL = "Default"

ACTION_DELETE = "delete"
ACTION_SETACTIVE = "setactive"
ACTION_UPSERT = "upsert"


class GhCredential(BaseModel):
    id: str
    label: str = DEFAULT_LABEL
    token: str = ""


class GhMetadataDoc(BaseModel):
    version: int = 1
    activeTokenId: str = ""
    tokens: list[GhCredential] = Field(default_factory=list)


class GhActionPayload(BaseModel):
    action: Literal["upsert", "delete", "setactive"]
    id: Optional[str] = None
    label: Optional[str] = None
    token: Optional[str] = None


def _default_doc() -> dict[str, Any]:
    return GhMetadataDoc().model_dump()


def validate_github_token(token: str) -> bool:
    """Ensure the GitHub token adheres strictly to standard secure prefixes (e.g. ghp_, github_pat_)."""
    t = (token or "").strip()
    if not t:
        return False
    # GitHub officially shifted to prefixed token patterns on April 5, 2021.
    if re.match(r"^(ghp|github_pat|ghs|gho|ghu)_[a-zA-Z0-9_]{10,}$", t):
        return True
    return False


def load_document(workspace_root: Any = None) -> dict[str, Any]:
    """Load metadata from ES. Returns empty default doc if ES is unavailable.

    AP-14: The workspace_root parameter is retained for call-site compatibility
    but is intentionally unused — all token metadata lives in Elasticsearch.
    """
    try:
        from es_credential_store import load_gh_tokens  # type: ignore
        doc = load_gh_tokens(_default_doc)
        if doc and (doc.get("tokens") or doc.get("activeTokenId")):
            return GhMetadataDoc(**doc).model_dump()
    except Exception as e:
        logger.warning("Failed to load GitHub tokens from ES — using defaults", extra={"structured_data": {"error": str(e)}})
    return _default_doc()


def save_document(workspace_root: Path, doc: dict[str, Any]) -> None:
    """Persist metadata to ES. Secrets (PAT) stay in OpenBao."""
    try:
        model = GhMetadataDoc(**doc)
    except ValidationError as e:
        logger.error("Invalid GitHub document structure during save", extra={"structured_data": {"error": str(e)}})
        return

    # Mask the token before saving
    masked_model = model.model_copy(deep=True)
    for cred in masked_model.tokens:
        if cred.token and cred.token not in (MASK, OPENBAO_DELEGATED_MASK):
            cred.token = OPENBAO_DELEGATED_MASK

    try:
        from es_credential_store import save_gh_tokens  # type: ignore
        # Use json dumps/loads to ensure clean serialization of pydantic dict
        save_gh_tokens(json.loads(masked_model.model_dump_json()))
    except Exception as e:
        logger.error("Failed to persist GitHub tokens to ES", extra={"structured_data": {"error": str(e)}})


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
    """If the JSON store is empty but GH_TOKEN exists in effective env, import one labeled row."""
    doc = GhMetadataDoc(**load_document(workspace_root))
    if doc.tokens:
        return
    try:
        from llm_settings import load_effective_pairs  # type: ignore
    except ImportError:
        return

    raw = _strip_env_quotes(load_effective_pairs(workspace_root).get(ENV_GH_TOKEN, "") or "")
    if not raw:
        return
    tid = uuid.uuid4().hex[:12]
    new_cred = GhCredential(id=tid, label=DEFAULT_LEGACY_LABEL, token=raw)
    doc.tokens.append(new_cred)
    doc.activeTokenId = tid
    save_document(workspace_root, doc.model_dump())


def _sync_active_to_env(workspace_root: Path) -> None:
    # AP-10: GitHub tokens are no longer written to .env at runtime.
    # Workers read the active token directly from ES (flume-github-tokens) + OpenBao
    # via get_active_token_plain(). This function is intentionally a no-op.
    pass


def get_active_token_id(workspace_root: Path) -> str:
    doc = GhMetadataDoc(**load_document(workspace_root))
    return doc.activeTokenId.strip()


def get_active_token_plain(workspace_root: Path) -> str:
    doc = GhMetadataDoc(**load_document(workspace_root))
    aid = doc.activeTokenId.strip()
    for c in doc.tokens:
        if c.id.strip() != aid:
            continue
        token = c.token.strip()
        if token == OPENBAO_DELEGATED_MASK:
            try:
                from llm_settings import _openbao_get_all  # type: ignore
                bao_vals = _openbao_get_all(workspace_root)
                delegated_token = str(bao_vals.get(f"FLUME_GH_{aid}") or "").strip()
                if delegated_token:
                    token = delegated_token
            except ImportError:
                logger.debug("OpenBao delegation import unavailable for GitHub token resolution")
        return token
    return ""


def list_public_tokens(workspace_root: Path) -> list[dict[str, Any]]:
    doc = GhMetadataDoc(**load_document(workspace_root))
    out: list[dict[str, Any]] = []
    for c in doc.tokens:
        tid = c.id.strip()
        if not tid:
            continue
        key = c.token.strip()
        out.append(
            {
                "id": tid,
                "label": c.label.strip() or tid,
                "tokenSuffix": _token_suffix(key),
                "hasToken": bool(key),
            }
        )
    return out


def _label_taken(tokens: list[GhCredential], label: str, exclude_id: Optional[str]) -> bool:
    ll = (label or "").strip().lower()
    if not ll:
        return False
    ex = (exclude_id or "").strip() or None
    for c in tokens:
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


def _handle_delete(workspace_root: Path, doc: GhMetadataDoc, payload: GhActionPayload) -> tuple[bool, str]:
    cid = (payload.id or "").strip()
    if not cid:
        return False, "id is required"
    
    new_toks = [c for c in doc.tokens if c.id != cid]
    if len(new_toks) == len(doc.tokens):
        return False, "GitHub token not found"
        
    doc.tokens = new_toks
    if doc.activeTokenId == cid:
        doc.activeTokenId = new_toks[0].id.strip() if new_toks else ""
        
    _safe_bao_put(workspace_root, f"FLUME_GH_{cid}", "", "GitHub token delete")
    
    save_document(workspace_root, doc.model_dump())
    _sync_active_to_env(workspace_root)
    return True, ""


def _handle_setactive(workspace_root: Path, doc: GhMetadataDoc, payload: GhActionPayload) -> tuple[bool, str]:
    cid = (payload.id or "").strip()
    if not cid:
        return False, "id is required"
        
    row = next((c for c in doc.tokens if c.id == cid), None)
    if not row:
        return False, "GitHub token not found"
    if not row.token.strip():
        return False, "Token has no secret — paste a PAT before setting active"
        
    doc.activeTokenId = cid
    save_document(workspace_root, doc.model_dump())
    _sync_active_to_env(workspace_root)
    return True, ""


def _handle_upsert(workspace_root: Path, doc: GhMetadataDoc, payload: GhActionPayload) -> tuple[bool, str]:
    label = (payload.label or "").strip()
    cred_id = (payload.id or "").strip() or None
    token_in = (payload.token or "").strip() if payload.token is not None else ""
    
    if cred_id:
        row = next((c for c in doc.tokens if c.id == cred_id), None)
        if not row:
            return False, "GitHub token not found"
        if label:
            if _label_taken(doc.tokens, label, cred_id):
                return False, f'Another token is already labeled "{label}"'
            row.label = label
            
        if token_in and token_in != MASK:
            if not validate_github_token(token_in):
                return False, "Invalid GitHub token format. Must begin with ghp_, github_pat_, ghs_, gho_, or ghu_."
            row.token = token_in
            
        if not doc.activeTokenId.strip() and row.token.strip():
            doc.activeTokenId = cred_id
            
        if token_in and token_in != MASK:
            _safe_bao_put(workspace_root, f"FLUME_GH_{cred_id}", token_in, "GitHub token upsert (update)")
            
        save_document(workspace_root, doc.model_dump())
        _sync_active_to_env(workspace_root)
        return True, ""

    # New credential
    if not label:
        label = DEFAULT_LABEL
    if _label_taken(doc.tokens, label, None):
        return False, f'Another token is already labeled "{label}"'
    if not token_in or token_in == MASK:
        return False, "token is required for new GitHub PATs"
    if not validate_github_token(token_in):
        return False, "Invalid GitHub token format. Must begin with ghp_, github_pat_, ghs_, gho_, or ghu_."
        
    new_id = uuid.uuid4().hex[:12]
    new_cred = GhCredential(id=new_id, label=label, token=token_in)
    doc.tokens.append(new_cred)
    
    if not doc.activeTokenId.strip():
        doc.activeTokenId = new_id
        
    if token_in and token_in != MASK:
        _safe_bao_put(workspace_root, f"FLUME_GH_{new_id}", token_in, "GitHub token upsert (new)")
        
    save_document(workspace_root, doc.model_dump())
    _sync_active_to_env(workspace_root)
    return True, ""


def apply_action(workspace_root: Path, body: dict[str, Any]) -> tuple[bool, str]:
    try:
        payload = GhActionPayload(**body)
    except ValidationError as e:
        return False, f"Invalid payload: {e}"

    doc = GhMetadataDoc(**load_document(workspace_root))

    if payload.action == ACTION_DELETE:
        return _handle_delete(workspace_root, doc, payload)
    if payload.action == ACTION_SETACTIVE:
        return _handle_setactive(workspace_root, doc, payload)
    if payload.action == ACTION_UPSERT:
        return _handle_upsert(workspace_root, doc, payload)

    return False, "githubTokenAction.action must be upsert, delete, or setActive"


def apply_legacy_gh_token_value(workspace_root: Path, token: str) -> tuple[bool, str]:
    """Single-field Settings save: set secret on active row, or create Default."""
    ensure_migrated_from_env(workspace_root)
    doc = GhMetadataDoc(**load_document(workspace_root))
    aid = doc.activeTokenId.strip()
    
    if aid:
        for c in doc.tokens:
            if c.id == aid:
                if token and token != MASK and not validate_github_token(token):
                    return False, "Invalid GitHub token format. Must begin with ghp_, github_pat_, ghs_, gho_, or ghu_."
                c.token = token
                if token and token != MASK:
                    _safe_bao_put(workspace_root, f"FLUME_GH_{aid}", token, "GitHub legacy value (active)")
                save_document(workspace_root, doc.model_dump())
                _sync_active_to_env(workspace_root)
                return True, ""
                
    new_id = uuid.uuid4().hex[:12]
    if token and token != MASK and not validate_github_token(token):
        return False, "Invalid GitHub token format. Must begin with ghp_, github_pat_, ghs_, gho_, or ghu_."
        
    new_cred = GhCredential(id=new_id, label=DEFAULT_LEGACY_LABEL, token=token)
    doc.tokens.append(new_cred)
    doc.activeTokenId = new_id
    
    if token and token != MASK:
        _safe_bao_put(workspace_root, f"FLUME_GH_{new_id}", token, "GitHub legacy value (new)")
        
    save_document(workspace_root, doc.model_dump())
    _sync_active_to_env(workspace_root)
    return True, ""
