"""
Kubernetes-grade Elasticsearch backend for credential metadata storage.

Replaces the local JSON file stores for llm_credentials, ado_tokens,
github_tokens, AND the .env file for LLM provider/model/base_url config.

Architecture:
  - Non-sensitive config  → ES index  (provider, model, baseUrl, label, etc.)
  - Secrets               → OpenBao KV at secret/data/flume/{store}/{id}

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
INDEX_LLM_CONFIG      = "flume-llm-config"       # AP-10: non-sensitive LLM settings (provider/model/baseUrl)
INDEX_LLM_CREDENTIALS = "flume-llm-credentials"
INDEX_ADO_TOKENS       = "flume-ado-tokens"
INDEX_GH_TOKENS        = "flume-github-tokens"

# Mapping for each index  (no apiKey / token fields — secrets stay in Vault)
_INDEX_MAPPINGS: dict[str, dict] = {
    INDEX_LLM_CONFIG: {
        "mappings": {
            "properties": {
                # Non-sensitive runtime LLM configuration — no secrets stored here
                "LLM_PROVIDER":  {"type": "keyword"},
                "LLM_MODEL":     {"type": "keyword"},
                "LLM_BASE_URL":  {"type": "keyword"},
                "LLM_ROUTE_TYPE":{"type": "keyword"},
            }
        }
    },
    INDEX_LLM_CREDENTIALS: {
        "mappings": {
            "properties": {
                "store_key":          {"type": "keyword"},
                "version":            {"type": "integer"},
                "activeCredentialId": {"type": "keyword"},
                "defaultCredentialId":{"type": "keyword"},
                "credentials":        {"type": "object", "enabled": False},
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
    """Execute an ES request and return the parsed JSON body.

    HEAD requests are not supported here — use _index_exists() instead.
    Returns None on 404 or any transport/parse error.
    """
    url = f"{_es_url()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_es_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        logger.warning(f"ES {method} {path} → HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
        return None
    except Exception as exc:
        logger.warning(f"ES {method} {path} error: {exc}")
        return None


def _index_exists(index: str, timeout: int = 5) -> bool:
    """Check whether an ES index exists using a HEAD request.

    HEAD responses have an empty body; we only inspect the HTTP status code.
    Returns True if 200, False if 404, and False on any other error.
    """
    url = f"{_es_url()}/{index}"
    req = urllib.request.Request(url, headers=_es_headers(), method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        logger.warning(f"ES HEAD /{index} → HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
        return False
    except Exception as exc:
        logger.warning(f"ES HEAD /{index} error: {exc}")
        return False


# ---------------------------------------------------------------------------
# Index bootstrap (idempotent — called from ensure_es_indices)
# ---------------------------------------------------------------------------

def ensure_credential_indices() -> None:
    """Create the three credential metadata indices if they don't already exist.

    es_bootstrap.py also registers these indices in REQUIRED_INDICES, so they
    may already exist before this function runs. The _index_exists() HEAD check
    avoids the spurious 400 resource_already_exists_exception warnings.
    """
    for index, mapping in _INDEX_MAPPINGS.items():
        if _index_exists(index):
            logger.debug(f"ES credential index already exists, skipping: {index}")
            continue
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

# ── LLM non-sensitive config (AP-10) ─────────────────────────────────────

def load_llm_config() -> dict[str, str]:
    """Load non-sensitive LLM settings (provider, model, baseUrl) from ES.

    Returns a plain {str: str} dict; callers get an empty dict on any failure
    (graceful degradation to .env / OpenBao fallback in load_effective_pairs).
    """
    result = _request("GET", f"/{INDEX_LLM_CONFIG}/_doc/{_DOC_ID}")
    if result and result.get("found"):
        src = result.get("_source", {})
        if isinstance(src, dict):
            return {str(k): str(v) for k, v in src.items() if v is not None}
    return {}


def save_llm_config(config: dict[str, str]) -> bool:
    """Persist non-sensitive LLM settings to ES.

    Only stores safe keys (LLM_PROVIDER, LLM_MODEL, LLM_BASE_URL, LLM_ROUTE_TYPE).
    Returns True on success.
    """
    SAFE_KEYS = frozenset({"LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_ROUTE_TYPE"})
    doc = {k: str(v) for k, v in config.items() if k in SAFE_KEYS and v is not None}
    result = _request("PUT", f"/{INDEX_LLM_CONFIG}/_doc/{_DOC_ID}", doc)
    return result is not None


# ── Credential stores ─────────────────────────────────────────────────────

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
