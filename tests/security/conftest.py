"""
Shared security test configuration.

All security tests run against the live Flume stack and validate
authorization enforcement, credential masking, and KMS integration.
"""
import os
import pytest
import httpx

FLUME_API_BASE = os.environ.get("FLUME_API_BASE", "http://localhost:8765/api")
FLUME_ES_URL = os.environ.get("FLUME_ES_URL", "https://localhost:9200")
FLUME_ES_PASSWORD = os.environ.get("FLUME_ELASTIC_PASSWORD", "")


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
    auth = ("elastic", FLUME_ES_PASSWORD) if FLUME_ES_PASSWORD else None
    with httpx.Client(
        base_url=FLUME_ES_URL,
        timeout=10.0,
        verify=False,
        auth=auth,
    ) as client:
        yield client
