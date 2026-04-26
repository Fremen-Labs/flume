"""
Shared Elasticsearch authentication and TLS helpers.

Single source of truth for ES credential propagation across all Flume
Python processes (dashboard, worker-manager, ast-poller, credential stores).

Auth priority:
  1. ES_API_KEY (when set and not a placeholder)
  2. FLUME_ELASTIC_PASSWORD (Basic Auth fallback for TLS-hardened clusters)

TLS: Returns an ssl.SSLContext with verify disabled (self-signed certs)
when ES_VERIFY_TLS != 'true'.
"""
from __future__ import annotations

import base64
import os
import ssl


def get_es_auth_headers() -> dict[str, str]:
    """Return the appropriate Authorization header for ES requests.

    Prefers API Key auth; falls back to Basic Auth via FLUME_ELASTIC_PASSWORD.
    Returns an empty dict if neither credential is available.
    """
    api_key = os.environ.get("ES_API_KEY", "").strip()
    if api_key and "bypass" not in api_key and api_key != "AUTO_GENERATED_BY_INSTALLER":
        return {"Authorization": f"ApiKey {api_key}"}

    es_pass = os.environ.get("FLUME_ELASTIC_PASSWORD", "").strip()
    if es_pass:
        b64 = base64.b64encode(f"elastic:{es_pass}".encode()).decode()
        return {"Authorization": f"Basic {b64}"}

    return {}


def get_es_ssl_context() -> ssl.SSLContext | None:
    """Return an SSL context appropriate for the ES connection.

    When ES_URL starts with https and ES_VERIFY_TLS is not 'true',
    returns a context that skips certificate verification (self-signed certs).
    Returns None when ES_URL uses plain HTTP.
    """
    es_url = os.environ.get("ES_URL", "")
    if not es_url.startswith("https"):
        return None

    verify = os.environ.get("ES_VERIFY_TLS", "false").lower() == "true"
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx
