"""
Root test configuration — Shared utilities for integration and security tests.

This conftest provides DRY helper functions and constants used by multiple
test suites that interact with the live Flume stack.
"""
import os
import subprocess
import json

import pytest
import httpx


# ── Shared Constants ─────────────────────────────────────────────────────────
FLUME_API_BASE = os.environ.get("FLUME_API_BASE", "http://localhost:8765/api")
FLUME_ES_URL = os.environ.get("FLUME_ES_URL", "https://localhost:9200")
FLUME_GATEWAY_URL = os.environ.get("FLUME_GATEWAY_URL", "http://localhost:8090")
FLUME_OPENBAO_URL = os.environ.get("FLUME_OPENBAO_URL", "http://localhost:8200")


# ── Shared Helpers ───────────────────────────────────────────────────────────

def get_elastic_password() -> str:
    """Fetch the ES elastic user password from the Go orchestrator.

    Always uses the fresh Go orchestrator snapshot via hidden _testenv
    command. Ignoring os.environ prevents stale passwords in the developer
    shell from causing 401 Unauthorized errors after a stack rebuild.
    """
    try:
        res = subprocess.run(
            ["./flume", "_testenv"],
            capture_output=True, text=True, check=True,
        )
        stdout = res.stdout
        start_idx = stdout.find('{')
        if start_idx != -1:
            env_cfg = json.loads(stdout[start_idx:])
            return env_cfg.get("ElasticPassword", "")
        return ""
    except Exception as e:
        print(f"Warning: Failed to fetch elastic password from orchestrator: {e}")
        return ""


def make_es_client(base_url: str = None, password: str = None) -> httpx.Client:
    """Create an httpx Client configured for the Flume ES instance.

    Uses HTTPS with self-signed cert verification disabled and Basic Auth
    via the provided password, matching the TLS-enabled docker-compose config.
    """
    url = base_url or FLUME_ES_URL
    auth = ("elastic", password) if password else None
    return httpx.Client(base_url=url, timeout=10.0, verify=False, auth=auth)
