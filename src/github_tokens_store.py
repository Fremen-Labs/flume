# Labeled GitHub PATs (multiple). Stored in worker-manager/github_tokens.json.
# The active token is mirrored to GH_TOKEN in .env / OpenBao for git clone and gh CLI.

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Optional

MASK = "***"
ENV_GH_TOKEN = "GH_TOKEN"


def tokens_path(workspace_root: Path) -> Path:
    return workspace_root / "worker-manager" / "github_tokens.json"


def _default_doc() -> dict[str, Any]:
    return {"version": 1, "activeTokenId": "", "tokens": []}


def load_document(workspace_root: Path) -> dict[str, Any]:
    path = tokens_path(workspace_root)
    if not path.is_file():
        return _default_doc()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_doc()
        data.setdefault("version", 1)
        data.setdefault("activeTokenId", "")
        tok = data.get("tokens")
        if not isinstance(tok, list):
            data["tokens"] = []
        return data
    except (OSError, json.JSONDecodeError):
        return _default_doc()


def save_document(workspace_root: Path, doc: dict[str, Any]) -> None:
    path = tokens_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


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
    from llm_settings import load_effective_pairs

    raw = _strip_env_quotes(load_effective_pairs(workspace_root).get(ENV_GH_TOKEN, "") or "")
    if not raw:
        return
    tid = uuid.uuid4().hex[:12]
    doc["tokens"] = [{"id": tid, "label": "Default", "token": raw}]
    doc["activeTokenId"] = tid
    save_document(workspace_root, doc)


def _sync_active_to_env(workspace_root: Path) -> None:
    from llm_settings import _update_env_keys

    plain = get_active_token_plain(workspace_root)
    _update_env_keys(workspace_root, {ENV_GH_TOKEN: plain})


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
        return str(c.get("token") or "").strip()
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
                row["token"] = token_in
            doc["tokens"] = tokens
            if not str(doc.get("activeTokenId") or "").strip() and str(row.get("token") or "").strip():
                doc["activeTokenId"] = cred_id
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
        new_id = uuid.uuid4().hex[:12]
        tokens.append({"id": new_id, "label": label, "token": token_in})
        doc["tokens"] = tokens
        if not str(doc.get("activeTokenId") or "").strip():
            doc["activeTokenId"] = new_id
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
                c["token"] = token
                doc["tokens"] = tokens
                save_document(workspace_root, doc)
                _sync_active_to_env(workspace_root)
                return True, ""
    new_id = uuid.uuid4().hex[:12]
    doc["tokens"] = tokens + [{"id": new_id, "label": "Default", "token": token}]
    doc["activeTokenId"] = new_id
    save_document(workspace_root, doc)
    _sync_active_to_env(workspace_root)
    return True, ""
