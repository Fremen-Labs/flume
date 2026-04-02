"""
Kubernetes-grade Elasticsearch backend for credential metadata storage.

Replaces the local JSON file stores for llm_credentials, ado_tokens, and
github_tokens. Only non-secret fields are persisted here. API keys and tokens
are delegated entirely to OpenBao KV (via the existing _openbao_put_many /
_openbao_get_all machinery in llm_settings.py).

Architecture:
  - Metadata  → ES index  (provider, label, baseUrl, isActive, etc.)
  - Secrets   → OpenBao KV at secret/data/flume/{store}/{id}

Callers continue to use the same load_document / save_document interface as
before; this module transparently provides the ES-backed implementation.
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from utils.logger import get_logger

logger = get_logger("es_credential_store")

# ---------------------------------------------------------------------------
# Index names
# ---------------------------------------------------------------------------
INDEX_LLM_CREDENTIALS = "flume-llm-credentials"
INDEX_ADO_TOKENS       = "flume-ado-tokens"
INDEX_GH_TOKENS        = "flume-github-tokens"

# Mapping for each index  (no apiKey / token fields — secrets stay in Vault)
_INDEX_MAPPINGS: dict[str, dict] = {
    INDEX_LLM_CREDENTIALS: {
        "mappings": {
            "properties": {
                "store_key":          {"type": "keyword"},   # "llm_credentials"
                "version":            {"type": "integer"},
                "activeCredentialId": {"type": "keyword"},
                "defaultCredentialId":{"type": "keyword"},
                "credentials":        {"type": "object", "enabled": False},  # nested list stored as JSON blob
            }
        }
    },
    INDEX_ADO_TOKENS: {
        "mappings": {
            "properties": {
                "store_key":          {"type": "keyword"},
                "version":            {"type": "integer"},
                "activeCredentialId": {"type": "keyword"},
                "credentials":        {"type": "object", "enabled": False},
            }
        }
    },
    INDEX_GH_TOKENS: {
        "mappings": {
            "properties": {
                "store_key":          {"type": "keyword"},
                "version":            {"type": "integer"},
                "activeTokenId":      {"type": "keyword"},
                "tokens":             {"type": "object", "enabled": False},
            }
        }
    },
}

# Singleton document ID inside each index (one doc per store)
_DOC_ID = "singleton"


# ---------------------------------------------------------------------------
# ES connection helpers (zero deps — uses stdlib urllib)
# ---------------------------------------------------------------------------

def _es_url() -> str:
    return os.environ.get("ES_URL", "http://elasticsearch:9200").rstrip("/")


def _es_headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    api_key = os.environ.get("ES_API_KEY", "")
    if api_key and "bypass" not in api_key:
        h["Authorization"] = f"ApiKey {api_key}"
    return h


def _request(method: str, path: str, body: Any = None, timeout: int = 5) -> dict[str, Any] | None:
    url = f"{_es_url()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_es_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        logger.warning(f"ES {method} {path} → HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
        return None
    except Exception as exc:
        logger.warning(f"ES {method} {path} error: {exc}")
        return None


# ---------------------------------------------------------------------------
# Index bootstrap (idempotent — called from ensure_es_indices)
# ---------------------------------------------------------------------------

def ensure_credential_indices() -> None:
    """Create the three credential metadata indices if they don't exist."""
    for index, mapping in _INDEX_MAPPINGS.items():
        # HEAD check
        check = _request("HEAD", f"/{index}")
        if check is not None:
            continue  # already exists
        result = _request("PUT", f"/{index}", mapping)
        if result is not None:
            logger.info(f"Created ES credential index: {index}")
        else:
            logger.warning(f"Failed to create ES credential index: {index}")


# ---------------------------------------------------------------------------
# Generic load / save that mirrors the JSON-file interface
# ---------------------------------------------------------------------------

def _scrub_secrets(doc: dict[str, Any]) -> dict[str, Any]:
    """
    Strip actual secret values before writing to ES. The secret placeholder
    '***OPENBAO_DELEGATED***' is kept so callers know a key exists.
    """
    scrubbed = dict(doc)

    def _clean_list(items: list[dict]) -> list[dict]:
        cleaned = []
        for item in items:
            row = dict(item)
            for secret_field in ("apiKey", "token", "pat", "password"):
                val = str(row.get(secret_field) or "").strip()
                if val and val != "***OPENBAO_DELEGATED***":
                    row[secret_field] = "***OPENBAO_DELEGATED***"
            cleaned.append(row)
        return cleaned

    for list_key in ("credentials", "tokens"):
        if isinstance(scrubbed.get(list_key), list):
            scrubbed[list_key] = _clean_list(scrubbed[list_key])

    return scrubbed


def load_from_es(index: str, default_factory: Any) -> dict[str, Any]:
    """Load the singleton metadata document from ES, falling back to default."""
    result = _request("GET", f"/{index}/_doc/{_DOC_ID}")
    if result and result.get("found"):
        src = result.get("_source", {})
        if isinstance(src, dict):
            return src
    return default_factory()


def save_to_es(index: str, doc: dict[str, Any]) -> None:
    """Write the (secret-scrubbed) metadata document to ES."""
    safe_doc = _scrub_secrets(doc)
    _request("PUT", f"/{index}/_doc/{_DOC_ID}", safe_doc)


# ---------------------------------------------------------------------------
# Store-specific helpers (public API used by the store modules)
# ---------------------------------------------------------------------------

def load_llm_credentials(default_factory: Any) -> dict[str, Any]:
    return load_from_es(INDEX_LLM_CREDENTIALS, default_factory)


def save_llm_credentials(doc: dict[str, Any]) -> None:
    save_to_es(INDEX_LLM_CREDENTIALS, doc)


def load_ado_tokens(default_factory: Any) -> dict[str, Any]:
    return load_from_es(INDEX_ADO_TOKENS, default_factory)


def save_ado_tokens(doc: dict[str, Any]) -> None:
    save_to_es(INDEX_ADO_TOKENS, doc)


def load_gh_tokens(default_factory: Any) -> dict[str, Any]:
    return load_from_es(INDEX_GH_TOKENS, default_factory)


def save_gh_tokens(doc: dict[str, Any]) -> None:
    save_to_es(INDEX_GH_TOKENS, doc)
