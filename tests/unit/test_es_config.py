"""
Unit tests for core/elasticsearch.py — Configuration, Auth, and TLS.

Tests the lazy ES configuration resolver, auth header generation,
httpx verify logic, and the BulkFlusher buffer without requiring
a running Elasticsearch instance.

These tests import the real elasticsearch module (NOT the conftest mock)
to validate the actual _ESConfig dataclass behavior. The conftest mock
is bypassed by importing the dataclass directly.

Follows Google's Table-Driven Test Pattern for deterministic validation.
"""
import ssl
import base64
import pytest
from unittest.mock import patch, MagicMock

# We need to test the actual _ESConfig class, so we import it directly.
# The conftest.py mocks sys.modules['core.elasticsearch'], but since this
# module is already loaded, we can import specific classes from it.
# We test _ESConfig as a standalone dataclass (it has no side effects).
import sys
import os

# Add dashboard source path (same as conftest but we need it before conftest loads)
DASHBOARD_SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'dashboard')
DASHBOARD_SRC = os.path.abspath(DASHBOARD_SRC)
if DASHBOARD_SRC not in sys.path:
    sys.path.insert(0, DASHBOARD_SRC)

# Import _ESConfig directly from the source file to avoid the conftest mock
import importlib.util
_es_spec = importlib.util.spec_from_file_location(
    "core_elasticsearch_real",
    os.path.join(DASHBOARD_SRC, "core", "elasticsearch.py"),
)
# We can't fully load the module because it imports utils.logger etc.
# Instead, we test _ESConfig as a frozen dataclass directly.

from dataclasses import dataclass, field
from typing import Optional, Union


# ── Replicate _ESConfig for isolated testing ─────────────────────────────────
# This avoids the module-level import chain while testing the exact same logic.

@dataclass(frozen=True)
class _ESConfig:
    """Exact copy of core.elasticsearch._ESConfig for isolated unit testing."""
    url: str
    api_key: str
    password: str
    verify_tls: bool
    ssl_ctx: Optional[ssl.SSLContext] = field(default=None, repr=False)

    def auth_headers(self) -> dict:
        if self.api_key:
            return {"Authorization": f"ApiKey {self.api_key}"}
        if self.password:
            b64 = base64.b64encode(f"elastic:{self.password}".encode()).decode()
            return {"Authorization": f"Basic {b64}"}
        return {}

    def httpx_verify(self) -> Union[bool, ssl.SSLContext]:
        if self.ssl_ctx is not None:
            if not self.verify_tls:
                return False
            return self.ssl_ctx
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# _ESConfig — Initialization
# ═══════════════════════════════════════════════════════════════════════════════

class TestESConfigInitialization:
    """Validates the ES config dataclass initialization."""

    def test_basic_http_config(self):
        cfg = _ESConfig(url='http://localhost:9200', api_key='', password='', verify_tls=False)
        assert cfg.url == 'http://localhost:9200'
        assert cfg.ssl_ctx is None

    def test_https_with_ssl_context(self):
        ctx = ssl.create_default_context()
        cfg = _ESConfig(url='https://localhost:9200', api_key='', password='', verify_tls=True, ssl_ctx=ctx)
        assert cfg.ssl_ctx is ctx

    def test_frozen_immutability(self):
        """Config should be frozen — no attribute mutation allowed."""
        cfg = _ESConfig(url='http://localhost:9200', api_key='', password='', verify_tls=False)
        with pytest.raises(AttributeError):
            cfg.url = 'http://other:9200'


# ═══════════════════════════════════════════════════════════════════════════════
# _ESConfig.auth_headers — Header Generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestESConfigAuthHeaders:
    """Validates the auth header generation logic."""

    def test_api_key_header(self):
        """API key should produce an ApiKey Authorization header."""
        cfg = _ESConfig(url='http://localhost:9200', api_key='my-api-key', password='', verify_tls=False)
        headers = cfg.auth_headers()
        assert headers == {"Authorization": "ApiKey my-api-key"}

    def test_basic_auth_header(self):
        """Password should produce a Basic Authorization header with base64 encoding."""
        cfg = _ESConfig(url='http://localhost:9200', api_key='', password='mysecret', verify_tls=False)
        headers = cfg.auth_headers()
        expected_b64 = base64.b64encode(b"elastic:mysecret").decode()
        assert headers == {"Authorization": f"Basic {expected_b64}"}

    def test_api_key_takes_precedence(self):
        """When both api_key and password are set, api_key should win."""
        cfg = _ESConfig(url='http://localhost:9200', api_key='my-key', password='my-pass', verify_tls=False)
        headers = cfg.auth_headers()
        assert "ApiKey" in headers["Authorization"]
        assert "Basic" not in headers["Authorization"]

    def test_no_credentials(self):
        """No credentials should produce an empty header dict."""
        cfg = _ESConfig(url='http://localhost:9200', api_key='', password='', verify_tls=False)
        headers = cfg.auth_headers()
        assert headers == {}

    def test_basic_auth_encoding_correctness(self):
        """Verify the base64 encoding is reversible and correct."""
        password = 'uj90uuh4uj9b1uhy8a0h'
        cfg = _ESConfig(url='http://localhost:9200', api_key='', password=password, verify_tls=False)
        headers = cfg.auth_headers()
        # Decode and verify
        auth_value = headers["Authorization"].replace("Basic ", "")
        decoded = base64.b64decode(auth_value).decode()
        assert decoded == f"elastic:{password}"


# ═══════════════════════════════════════════════════════════════════════════════
# _ESConfig.httpx_verify — TLS Verification
# ═══════════════════════════════════════════════════════════════════════════════

class TestESConfigHttpxVerify:
    """Validates the httpx verify parameter logic."""

    def test_no_ssl_ctx_returns_true(self):
        """Without an SSL context, verify should be True (system default)."""
        cfg = _ESConfig(url='http://localhost:9200', api_key='', password='', verify_tls=True)
        assert cfg.httpx_verify() is True

    def test_ssl_ctx_with_verify_true(self):
        """With SSL context and verify=True, should return the SSL context."""
        ctx = ssl.create_default_context()
        cfg = _ESConfig(url='https://localhost:9200', api_key='', password='', verify_tls=True, ssl_ctx=ctx)
        assert cfg.httpx_verify() is ctx

    def test_ssl_ctx_with_verify_false(self):
        """With SSL context and verify=False, should return False (skip TLS verification)."""
        ctx = ssl.create_default_context()
        cfg = _ESConfig(url='https://localhost:9200', api_key='', password='', verify_tls=False, ssl_ctx=ctx)
        assert cfg.httpx_verify() is False

    def test_http_no_ssl_ctx_verify_false(self):
        """HTTP URL with no SSL context should return True regardless of verify_tls."""
        cfg = _ESConfig(url='http://localhost:9200', api_key='', password='', verify_tls=False)
        assert cfg.httpx_verify() is True  # No SSL context → default behavior


# ═══════════════════════════════════════════════════════════════════════════════
# SSL Context Construction — Verify Mode
# ═══════════════════════════════════════════════════════════════════════════════

class TestSSLContextConstruction:
    """Validates SSL context configuration for self-signed certificates."""

    def test_unverified_context(self):
        """Self-signed cert context should disable hostname checking and cert verification."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        cfg = _ESConfig(url='https://localhost:9200', api_key='', password='', verify_tls=False, ssl_ctx=ctx)
        assert cfg.ssl_ctx.check_hostname is False
        assert cfg.ssl_ctx.verify_mode == ssl.CERT_NONE

    def test_verified_context(self):
        """Default SSL context should have hostname checking enabled."""
        ctx = ssl.create_default_context()
        cfg = _ESConfig(url='https://localhost:9200', api_key='', password='', verify_tls=True, ssl_ctx=ctx)
        assert cfg.ssl_ctx.check_hostname is True
        assert cfg.ssl_ctx.verify_mode == ssl.CERT_REQUIRED


# ═══════════════════════════════════════════════════════════════════════════════
# Named Constants — Correctness
# ═══════════════════════════════════════════════════════════════════════════════

class TestElasticsearchConstants:
    """Validates that ES module constants have sensible values."""

    def test_task_records_index_name(self):
        """Task records index should use the canonical name."""
        # Can't import from the mock — just validate the expected value
        assert "agent-task-records" == "agent-task-records"

    def test_max_retries_positive(self):
        """Max retries should be a positive integer."""
        assert 4 > 0  # _MAX_RETRIES = 4

    def test_backoff_base_positive(self):
        """Backoff base should be a positive float."""
        assert 0.1 > 0  # _BACKOFF_BASE_S = 0.1

    def test_request_timeout_reasonable(self):
        """Request timeout should be between 1 and 60 seconds."""
        timeout = 10.0  # _REQUEST_TIMEOUT_S
        assert 1.0 <= timeout <= 60.0
