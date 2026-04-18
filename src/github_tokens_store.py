# Labeled GitHub PATs (multiple).
# Metadata stored in ES index 'flume-github-tokens'.
# PATs stored exclusively in OpenBao KV at secret/data/flume/github_tokens/{id}.
# AP-14: Local JSON fallback removed — ES is the sole metadata store.

from __future__ import annotations

import re
import json
import uuid
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

logger = get_logger("github_tokens_store")

MASK = "***"
ENV_GH_TOKEN = "GH_TOKEN"


def _default_doc() -> dict[str, Any]:
    return {"version": 1, "activeTokenId": "", "tokens": []}

def validate_github_token(token: str) -> bool:
    """Ensure the GitHub token adheres strictly to standard secure prefixes (e.g. ghp_, github_pat_)."""
    t = (token or "").strip()
    if not t:
        return False
    # GitHub officially shifted to prefixed token patterns on April 5, 2021.
    if re.match(r"^(ghp|github_pat|ghs|gho|ghu)_[a-zA-Z0-9_]{10,}$", t):
        return True
    return False



def load_document(workspace_root=None) -> dict[str, Any]:
    """Load metadata from ES. Returns empty default doc if ES is unavailable.

    AP-14: The workspace_root parameter is retained for call-site compatibility
    but is intentionally unused — all token metadata lives in Elasticsearch.
    """
    try:
        from es_credential_store import load_gh_tokens  # type: ignore
        doc = load_gh_tokens(_default_doc)
        if doc and (doc.get("tokens") or doc.get("activeTokenId")):
            doc.setdefault("version", 1)
            if not isinstance(doc.get("tokens"), list):
                doc["tokens"] = []
            return doc
    except Exception as e:
        logger.warning("Failed to load GitHub tokens from ES — using defaults", extra={"structured_data": {"error": str(e)}})
    return _default_doc()


def save_document(workspace_root: Path, doc: dict[str, Any]) -> None:
    """Persist metadata to ES. Secrets (PAT) stay in OpenBao."""
    masked_doc = json.loads(json.dumps(doc))
    for tok in masked_doc.get("tokens", []):
        if tok.get("token") and tok["token"] not in ("", MASK, "***OPENBAO_DELEGATED***"):
            tok["token"] = "***OPENBAO_DELEGATED***"
    try:
        from es_credential_store import save_gh_tokens  # type: ignore
        save_gh_tokens(masked_doc)
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
    doc = load_document(workspace_root)
    if doc.get("tokens"):
        return
    from llm_settings import load_effective_pairs  # type: ignore

    raw = _strip_env_quotes(load_effective_pairs(workspace_root).get(ENV_GH_TOKEN, "") or "")
    if not raw:
        return
    tid = uuid.uuid4().hex[:12]
    doc["tokens"] = [{"id": tid, "label": "Default", "token": raw}]
    doc["activeTokenId"] = tid
    save_document(workspace_root, doc)


def _sync_active_to_env(workspace_root: Path) -> None:
    # AP-10: GitHub tokens are no longer written to .env at runtime.
    # Workers read the active token directly from ES (flume-github-tokens) + OpenBao
    # via get_active_token_plain(). This function is intentionally a no-op.
    pass


def get_active_token_id(workspace_root: Path) -> str:
    return str(load_document(workspace_root).get("activeTokenId") or "").strip()


def get_active_token_plain(workspace_root: Path) -> str:
    doc = load_document(workspace_root)
    aid = str(doc.get("activeTokenId") or "").strip()
    for c in doc.get("tokens") or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("id") or "").strip() != aid:
            continue
        token = str(c.get("token") or "").strip()
        if token == "***OPENBAO_DELEGATED***":
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
    out: list[dict[str, Any]] = []
    for c in load_document(workspace_root).get("tokens") or []:
        if not isinstance(c, dict):
            continue
        tid = str(c.get("id") or "").strip()
        if not tid:
            continue
        key = str(c.get("token") or "").strip()
        out.append(
            {
                "id": tid,
                "label": str(c.get("label") or tid).strip() or tid,
                "tokenSuffix": _token_suffix(key),
                "hasToken": bool(key),
            }
        )
    return out


def _label_taken(tokens: list[dict[str, Any]], label: str, exclude_id: Optional[str]) -> bool:
    ll = (label or "").strip().lower()
    if not ll:
        return False
    ex = (exclude_id or "").strip() or None
    for c in tokens:
        cid = str(c.get("id") or "").strip()
        if ex and cid == ex:
            continue
        if str(c.get("label") or "").strip().lower() == ll:
            return True
    return False


def apply_action(workspace_root: Path, body: dict[str, Any]) -> tuple[bool, str]:
    action = str(body.get("action") or "").strip().lower()
    doc = load_document(workspace_root)
    tokens: list[dict[str, Any]] = []
    for c in doc.get("tokens") or []:
        if isinstance(c, dict) and c.get("id"):
            tokens.append(dict(c))

    if action == "delete":
        cid = str(body.get("id") or "").strip()
        if not cid:
            return False, "id is required"
        new_toks = [c for c in tokens if str(c.get("id")) != cid]
        if len(new_toks) == len(tokens):
            return False, "GitHub token not found"
        doc["tokens"] = new_toks
        if str(doc.get("activeTokenId") or "") == cid:
            doc["activeTokenId"] = str(new_toks[0].get("id") or "").strip() if new_toks else ""
        try:
            from llm_settings import _openbao_put_many  # type: ignore
            _openbao_put_many(workspace_root, {f"FLUME_GH_{cid}": ""})
        except ImportError:
            logger.debug("OpenBao delegation import unavailable during GitHub token delete")
        save_document(workspace_root, doc)
        _sync_active_to_env(workspace_root)
        return True, ""

    if action == "setactive":
        cid = str(body.get("id") or "").strip()
        if not cid:
            return False, "id is required"
        row = next((c for c in tokens if str(c.get("id")) == cid), None)
        if not row:
            return False, "GitHub token not found"
        if not str(row.get("token") or "").strip():
            return False, "Token has no secret — paste a PAT before setting active"
        doc["activeTokenId"] = cid
        doc["tokens"] = tokens
        save_document(workspace_root, doc)
        _sync_active_to_env(workspace_root)
        return True, ""

    if action == "upsert":
        label = str(body.get("label") or "").strip()
        cred_id = str(body.get("id") or "").strip() or None
        token_in = str(body.get("token") or "").strip()
        if cred_id:
            row = next((c for c in tokens if str(c.get("id")) == cred_id), None)
            if not row:
                return False, "GitHub token not found"
            if label:
                if _label_taken(tokens, label, cred_id):
                    return False, f'Another token is already labeled "{label}"'
                row["label"] = label
            if token_in and token_in != MASK:
                if not validate_github_token(token_in):
                    return False, "Invalid GitHub token format. Must begin with ghp_, github_pat_, ghs_, gho_, or ghu_."
                row["token"] = token_in
            doc["tokens"] = tokens
            if not str(doc.get("activeTokenId") or "").strip() and str(row.get("token") or "").strip():
                doc["activeTokenId"] = cred_id
            if token_in and token_in != MASK:
                try:
                    from llm_settings import _openbao_put_many  # type: ignore
                    _openbao_put_many(workspace_root, {f"FLUME_GH_{cred_id}": token_in})
                except ImportError:
                    logger.debug("OpenBao put unavailable during GitHub token upsert (update)")
            save_document(workspace_root, doc)
            _sync_active_to_env(workspace_root)
            return True, ""

        # New row
        if not label:
            label = "GitHub PAT"
        if _label_taken(tokens, label, None):
            return False, f'Another token is already labeled "{label}"'
        if not token_in or token_in == MASK:
            return False, "token is required for new GitHub PATs"
        if not validate_github_token(token_in):
            return False, "Invalid GitHub token format. Must begin with ghp_, github_pat_, ghs_, gho_, or ghu_."
        new_id = uuid.uuid4().hex[:12]
        tokens.append({"id": new_id, "label": label, "token": token_in})
        doc["tokens"] = tokens
        if not str(doc.get("activeTokenId") or "").strip():
            doc["activeTokenId"] = new_id
        if token_in and token_in != MASK:
            try:
                from llm_settings import _openbao_put_many  # type: ignore
                _openbao_put_many(workspace_root, {f"FLUME_GH_{new_id}": token_in})
            except ImportError:
                logger.debug("OpenBao put unavailable during GitHub token upsert (new)")
        save_document(workspace_root, doc)
        _sync_active_to_env(workspace_root)
        return True, ""

    return False, "githubTokenAction.action must be upsert, delete, or setActive"


def apply_legacy_gh_token_value(workspace_root: Path, token: str) -> tuple[bool, str]:
    """Single-field Settings save: set secret on active row, or create Default."""
    ensure_migrated_from_env(workspace_root)
    doc = load_document(workspace_root)
    tokens = [dict(c) for c in doc.get("tokens") or [] if isinstance(c, dict) and c.get("id")]
    aid = str(doc.get("activeTokenId") or "").strip()
    if aid:
        for c in tokens:
            if str(c.get("id")) == aid:
                if token and token != MASK and not validate_github_token(token):
                    return False, "Invalid GitHub token format. Must begin with ghp_, github_pat_, ghs_, gho_, or ghu_."
                c["token"] = token
                doc["tokens"] = tokens
                if token and token != MASK:
                    try:
                        from llm_settings import _openbao_put_many  # type: ignore
                        _openbao_put_many(workspace_root, {f"FLUME_GH_{aid}": token})
                    except ImportError:
                        logger.debug("OpenBao put unavailable during GitHub legacy value (active)")
                save_document(workspace_root, doc)
                _sync_active_to_env(workspace_root)
                return True, ""
    new_id = uuid.uuid4().hex[:12]
    if token and token != MASK and not validate_github_token(token):
        return False, "Invalid GitHub token format. Must begin with ghp_, github_pat_, ghs_, gho_, or ghu_."
    doc["tokens"] = tokens + [{"id": new_id, "label": "Default", "token": token}]
    doc["activeTokenId"] = new_id
    if token and token != MASK:
        try:
            from llm_settings import _openbao_put_many  # type: ignore
            _openbao_put_many(workspace_root, {f"FLUME_GH_{new_id}": token})
        except ImportError:
            logger.debug("OpenBao put unavailable during GitHub legacy value (new)")
    save_document(workspace_root, doc)
    _sync_active_to_env(workspace_root)
    return True, ""
