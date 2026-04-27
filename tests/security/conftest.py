"""
Shared security test configuration.

All security tests run against the live Flume stack and validate
authorization enforcement, credential masking, and KMS integration.
"""
import os
import pytest
import httpx

import subprocess
import json

FLUME_API_BASE = os.environ.get("FLUME_API_BASE", "http://localhost:8765/api")
FLUME_ES_URL = os.environ.get("FLUME_ES_URL", "https://localhost:9200")

def _get_elastic_password() -> str:
    try:
        res = subprocess.run(["./flume", "_testenv"], capture_output=True, text=True, check=True)
        stdout = res.stdout
        start_idx = stdout.find('{')
        if start_idx != -1:
            env_cfg = json.loads(stdout[start_idx:])
            return env_cfg.get("ElasticPassword", "")
        return ""
    except Exception as e:
        print(f"Warning: Failed to fetch elastic password from orchestrator: {e}")
        return ""

FLUME_ES_PASSWORD = _get_elastic_password()


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
