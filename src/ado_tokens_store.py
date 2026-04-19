# Labeled Azure DevOps credentials (PAT + org URL per row).
# Metadata stored in ES index 'flume-ado-tokens'.
# PATs stored exclusively in OpenBao KV at secret/data/flume/ado_tokens/{id}.
# AP-14: Local JSON fallback removed — ES is the sole metadata store.

from __future__ import annotations

import json
import uuid
from typing import Any, Optional
from pathlib import Path

from utils.logger import get_logger

logger = get_logger("ado_tokens_store")

MASK = "***"
ENV_ADO_TOKEN = "ADO_TOKEN"
ENV_ADO_ORG_URL = "ADO_ORG_URL"


def _default_doc() -> dict[str, Any]:
    return {"version": 1, "activeCredentialId": "", "credentials": []}



def load_document(workspace_root=None) -> dict[str, Any]:
    """Load metadata from ES. Returns empty default doc if ES is unavailable.

    AP-14: The workspace_root parameter is retained for call-site compatibility
    but is intentionally unused — all credential metadata lives in Elasticsearch.
    """
    try:
        from es_credential_store import load_ado_tokens
        doc = load_ado_tokens(_default_doc)
        if doc and (doc.get("credentials") or doc.get("activeCredentialId")):
            doc.setdefault("version", 1)
            if not isinstance(doc.get("credentials"), list):
                doc["credentials"] = []
            return doc
    except Exception as e:
        logger.warning("Failed to load ADO tokens from ES — using defaults", extra={"structured_data": {"error": str(e)}})
    return _default_doc()


def save_document(workspace_root: Path, doc: dict[str, Any]) -> None:
    """Persist metadata to ES. Secrets (PAT) stay in OpenBao."""
    masked_doc = json.loads(json.dumps(doc))
    for tok in masked_doc.get("credentials", []):
        if tok.get("token") and tok["token"] not in ("", MASK, "***OPENBAO_DELEGATED***"):
            tok["token"] = "***OPENBAO_DELEGATED***"
    try:
        from es_credential_store import save_ado_tokens
        save_ado_tokens(masked_doc)
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
    doc = load_document(workspace_root)
    if doc.get("credentials"):
        return
    from llm_settings import load_effective_pairs  # type: ignore

    pairs = load_effective_pairs(workspace_root)
    raw_tok = _strip_env_quotes(pairs.get(ENV_ADO_TOKEN, "") or "")
    raw_org = str(pairs.get(ENV_ADO_ORG_URL, "") or "").strip()
    if not raw_tok and not raw_org:
        return
    cid = uuid.uuid4().hex[:12]
    doc["credentials"] = [{"id": cid, "label": "Default", "token": raw_tok, "orgUrl": raw_org}]
    doc["activeCredentialId"] = cid if raw_tok else ""
    save_document(workspace_root, doc)


def _sync_active_to_env(workspace_root: Path) -> None:
    # AP-10: ADO tokens are no longer written to .env at runtime.
    # Workers read the active token directly from ES (flume-ado-tokens) + OpenBao
    # via get_active_token_plain(). This function is intentionally a no-op.
    pass


def get_active_credential_id(workspace_root: Path) -> str:
    return str(load_document(workspace_root).get("activeCredentialId") or "").strip()


def get_active_token_plain(workspace_root: Path) -> str:
    doc = load_document(workspace_root)
    aid = str(doc.get("activeCredentialId") or "").strip()
    for c in doc.get("credentials") or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("id") or "").strip() != aid:
            continue
        token = str(c.get("token") or "").strip()
        if token == "***OPENBAO_DELEGATED***":
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
    doc = load_document(workspace_root)
    aid = str(doc.get("activeCredentialId") or "").strip()
    for c in doc.get("credentials") or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("id") or "").strip() != aid:
            continue
        return str(c.get("orgUrl") or "").strip()
    return ""


def list_public_credentials(workspace_root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in load_document(workspace_root).get("credentials") or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if not cid:
            continue
        key = str(c.get("token") or "").strip()
        out.append(
            {
                "id": cid,
                "label": str(c.get("label") or cid).strip() or cid,
                "orgUrl": str(c.get("orgUrl") or "").strip(),
                "tokenSuffix": _token_suffix(key),
                "hasToken": bool(key),
            }
        )
    return out


def _label_taken(creds: list[dict[str, Any]], label: str, exclude_id: Optional[str]) -> bool:
    ll = (label or "").strip().lower()
    if not ll:
        return False
    ex = (exclude_id or "").strip() or None
    for c in creds:
        cid = str(c.get("id") or "").strip()
        if ex and cid == ex:
            continue
        if str(c.get("label") or "").strip().lower() == ll:
            return True
    return False


def apply_action(workspace_root: Path, body: dict[str, Any]) -> tuple[bool, str]:
    action = str(body.get("action") or "").strip().lower()
    doc = load_document(workspace_root)
    creds: list[dict[str, Any]] = []
    for c in doc.get("credentials") or []:
        if isinstance(c, dict) and c.get("id"):
            creds.append(dict(c))

    if action == "delete":
        cid = str(body.get("id") or "").strip()
        if not cid:
            return False, "id is required"
        new_c = [c for c in creds if str(c.get("id")) != cid]
        if len(new_c) == len(creds):
            return False, "ADO credential not found"
        doc["credentials"] = new_c
        if str(doc.get("activeCredentialId") or "") == cid:
            doc["activeCredentialId"] = str(new_c[0].get("id") or "").strip() if new_c else ""
        try:
            from llm_settings import _openbao_put_many  # type: ignore
            _openbao_put_many(workspace_root, {f"FLUME_ADO_{cid}": ""})
        except ImportError:
            logger.debug("OpenBao delegation import unavailable during ADO token delete")
        save_document(workspace_root, doc)
        _sync_active_to_env(workspace_root)
        return True, ""

    if action == "setactive":
        cid = str(body.get("id") or "").strip()
        if not cid:
            return False, "id is required"
        row = next((c for c in creds if str(c.get("id")) == cid), None)
        if not row:
            return False, "ADO credential not found"
        if not str(row.get("token") or "").strip():
            return False, "PAT is empty — paste a token before setting active"
        doc["activeCredentialId"] = cid
        doc["credentials"] = creds
        save_document(workspace_root, doc)
        _sync_active_to_env(workspace_root)
        return True, ""

    if action == "upsert":
        label = str(body.get("label") or "").strip()
        cred_id = str(body.get("id") or "").strip() or None
        has_token_key = "token" in body
        token_raw = body.get("token")
        token_in = str(token_raw).strip() if token_raw is not None else ""
        has_org_key = "orgUrl" in body
        org_raw = body.get("orgUrl")
        org_in = str(org_raw).strip() if org_raw is not None else ""

        if cred_id:
            row = next((c for c in creds if str(c.get("id")) == cred_id), None)
            if not row:
                return False, "ADO credential not found"
            if label:
                if _label_taken(creds, label, cred_id):
                    return False, f'Another credential is already labeled "{label}"'
                row["label"] = label
            if has_token_key:
                if token_in != MASK:
                    row["token"] = token_in
            if has_org_key:
                row["orgUrl"] = org_in
            doc["credentials"] = creds
            if not str(doc.get("activeCredentialId") or "").strip() and str(row.get("token") or "").strip():
                doc["activeCredentialId"] = cred_id
            if has_token_key and token_in and token_in != MASK:
                try:
                    from llm_settings import _openbao_put_many  # type: ignore
                    _openbao_put_many(workspace_root, {f"FLUME_ADO_{cred_id}": token_in})
                except ImportError:
                    logger.debug("OpenBao put unavailable during ADO token upsert (update)")
            save_document(workspace_root, doc)
            _sync_active_to_env(workspace_root)
            return True, ""

        # New credential — PAT + org URL required together
        if not label:
            label = "Azure DevOps"
        if _label_taken(creds, label, None):
            return False, f'Another credential is already labeled "{label}"'
        if not token_in or token_in == MASK:
            return False, "PAT is required for new ADO credentials"
        if not org_in:
            return False, "Organization URL is required when adding ADO credentials (pair with PAT)"
        new_id = uuid.uuid4().hex[:12]
        creds.append({"id": new_id, "label": label, "token": token_in, "orgUrl": org_in})
        doc["credentials"] = creds
        if not str(doc.get("activeCredentialId") or "").strip():
            doc["activeCredentialId"] = new_id
        if token_in and token_in != MASK:
            try:
                from llm_settings import _openbao_put_many  # type: ignore
                _openbao_put_many(workspace_root, {f"FLUME_ADO_{new_id}": token_in})
            except ImportError:
                logger.debug("OpenBao put unavailable during ADO token upsert (new)")
        save_document(workspace_root, doc)
        _sync_active_to_env(workspace_root)
        return True, ""

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
    doc = load_document(workspace_root)
    creds = [dict(c) for c in doc.get("credentials") or [] if isinstance(c, dict) and c.get("id")]
    aid = str(doc.get("activeCredentialId") or "").strip()

    def apply_to_row(row: dict[str, Any]) -> None:
        if update_token:
            row["token"] = token
        if update_org:
            row["orgUrl"] = org_url

    if aid:
        for c in creds:
            if str(c.get("id")) == aid:
                apply_to_row(c)
                doc["credentials"] = creds
                if update_token and token and token != MASK:
                    try:
                        from llm_settings import _openbao_put_many  # type: ignore
                        _openbao_put_many(workspace_root, {f"FLUME_ADO_{aid}": token})
                    except ImportError:
                        logger.debug("OpenBao put unavailable during ADO legacy patch (active)")
                save_document(workspace_root, doc)
                _sync_active_to_env(workspace_root)
                return True, ""

    new_id = uuid.uuid4().hex[:12]
    row: dict[str, Any] = {"id": new_id, "label": "Default", "token": "", "orgUrl": ""}
    apply_to_row(row)
    creds.append(row)
    doc["credentials"] = creds
    doc["activeCredentialId"] = new_id if str(row.get("token") or "").strip() else ""
    if update_token and token and token != MASK:
        try:
            from llm_settings import _openbao_put_many  # type: ignore
            _openbao_put_many(workspace_root, {f"FLUME_ADO_{new_id}": token})
        except ImportError:
            logger.debug("OpenBao put unavailable during ADO legacy patch (new)")
    save_document(workspace_root, doc)
    _sync_active_to_env(workspace_root)
    return True, ""
