"""
Security tests for KMS masking and credential sanitization.

Validates that sensitive credentials (API keys, vault tokens, secrets)
are never exposed through API responses. All key values must be masked
with placeholder strings like '***', '••••', or 'secured'.

Follows OWASP Sensitive Data Exposure (A3:2017) testing guidance.

Requires: Flume stack running (./flume start)
"""
import re
import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Settings Endpoint Masking — /api/settings/system
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.security
class TestSettingsSecretMasking:
    """Validates that GET /api/settings/system masks all sensitive fields."""

    def test_es_api_key_is_masked(self, api_client):
        """ES API key must be '***' — never the raw key."""
        data = api_client.get("/settings/system").json()
        assert data["es_api_key"] in ("***", ""), (
            f"ES API key exposed in settings response: '{data['es_api_key']}'"
        )

    def test_vault_token_is_masked(self, api_client):
        """Vault token must be '••••' or empty — never the raw token."""
        data = api_client.get("/settings/system").json()
        vault_token = data.get("vault_token", "")
        assert vault_token in ("••••", ""), (
            f"Vault token exposed in settings response: '{vault_token}'"
        )

    def test_es_url_is_not_masked(self, api_client):
        """ES URL should NOT be masked — it's non-sensitive infrastructure config."""
        data = api_client.get("/settings/system").json()
        assert data["es_url"] != "***"
        assert "://" in data["es_url"]  # Should be a real URL

    def test_no_raw_api_key_patterns_in_response(self, api_client):
        """Response body must not contain raw API key patterns like sk-*, gsk-*, xoxb-*."""
        resp = api_client.get("/settings/system")
        body = resp.text
        # Check for common API key prefixes
        dangerous_patterns = [
            r'\bsk-[a-zA-Z0-9]{10,}',   # OpenAI keys
            r'\bgsk_[a-zA-Z0-9]{10,}',   # Groq keys
            r'\bxoxb-[a-zA-Z0-9]{10,}',  # Slack tokens
            r'\bghp_[a-zA-Z0-9]{10,}',   # GitHub tokens
        ]
        for pattern in dangerous_patterns:
            assert not re.search(pattern, body), (
                f"Raw API key pattern found in settings response: {pattern}"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Security Endpoint — /api/security
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.security
class TestSecurityEndpointMasking:
    """Validates the /api/security endpoint never leaks credential values."""

    def test_openbao_keys_show_status_only(self, api_client):
        """Each key in openbao_keys must be 'secured', not the actual value."""
        data = api_client.get("/security").json()
        for key, value in data.get("openbao_keys", {}).items():
            assert value == "secured", (
                f"KMS key '{key}' value is exposed as '{value}' instead of 'secured'"
            )

    def test_no_raw_token_in_audit_logs(self, api_client):
        """Audit log entries must not contain raw secret values."""
        data = api_client.get("/security").json()
        for entry in data.get("audit_logs", []):
            msg = str(entry)
            # Must not contain long hex strings that look like tokens
            assert not re.search(r'[a-f0-9]{32,}', msg), (
                f"Possible raw token in audit log: {msg[:100]}"
            )

    def test_security_response_shape(self, api_client):
        """Security endpoint must have vault_active, openbao_keys, audit_logs."""
        data = api_client.get("/security").json()
        assert "vault_active" in data
        assert "openbao_keys" in data
        assert "audit_logs" in data


# ═══════════════════════════════════════════════════════════════════════════════
# Snapshot Credential Leak Check
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.security
class TestSnapshotCredentialLeakPrevention:
    """Validates the /api/snapshot response contains no leaked credentials."""

    def test_snapshot_contains_no_api_key_patterns(self, api_client):
        """Full snapshot body must not contain raw API key patterns."""
        resp = api_client.get("/snapshot")
        body = resp.text
        dangerous_patterns = [
            r'\bsk-[a-zA-Z0-9]{20,}',
            r'\bgsk_[a-zA-Z0-9]{20,}',
            r'\bBearer [a-zA-Z0-9]{20,}',
        ]
        for pattern in dangerous_patterns:
            assert not re.search(pattern, body), (
                f"Credential pattern found in snapshot: {pattern}"
            )

    def test_workers_do_not_expose_credentials(self, api_client):
        """Worker entries must not contain credential_id values that are raw keys."""
        data = api_client.get("/snapshot").json()
        for worker in data.get("workers", []):
            cred_id = worker.get("llm_credential_id", "")
            # Credential IDs should be symbolic labels, not raw keys
            assert not cred_id.startswith("sk-"), (
                f"Worker '{worker['name']}' exposes raw credential: {cred_id}"
            )
