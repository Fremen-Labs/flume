"""
Shared security test configuration.

All security tests run against the live Flume stack and validate
authorization enforcement, credential masking, and KMS integration.
"""
import pytest
import httpx

# ── DRY imports from root conftest ───────────────────────────────────────────
from tests.conftest import (
    FLUME_API_BASE,
    FLUME_ES_URL,
    get_elastic_password,
    make_es_client,
)

FLUME_ES_PASSWORD = get_elastic_password()


@pytest.fixture(scope="session")
def api_client():
    """Session-scoped HTTP client bound to the Flume Dashboard API."""
    with httpx.Client(base_url=FLUME_API_BASE, timeout=15.0) as client:
        yield client


@pytest.fixture(scope="session")
def es_client():
    """Session-scoped HTTP client bound to Elasticsearch.

    Uses HTTPS with self-signed cert verification disabled and Basic Auth
    via FLUME_ELASTIC_PASSWORD, matching TLS-enabled docker-compose config.
    """
    client = make_es_client(password=FLUME_ES_PASSWORD)
    yield client
    client.close()
