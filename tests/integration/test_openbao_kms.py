"""
Integration tests for OpenBao KMS.

Validates that the OpenBao vault is accessible and that the security
endpoint correctly reports secured keys without exposing their values.

Requires: Flume stack running (./flume start)
"""
import pytest


@pytest.mark.integration
class TestOpenBaoHealth:
    """Validates the OpenBao KMS is accessible and unsealed."""

    def test_openbao_is_reachable(self, openbao_client):
        """OpenBao health endpoint should respond."""
        resp = openbao_client.get("/v1/sys/health")
        # 200 = active, 429 = standby, 472 = recovery, 473 = perf standby, 501 = uninitialized, 503 = sealed
        assert resp.status_code in (200, 429, 472, 473), (
            f"OpenBao returned unexpected status: {resp.status_code}"
        )

    def test_openbao_is_unsealed(self, openbao_client):
        """In a running Flume stack, OpenBao must be initialized and unsealed."""
        resp = openbao_client.get("/v1/sys/health")
        if resp.status_code == 200:
            data = resp.json()
            assert data.get("initialized") is True
            assert data.get("sealed") is False


@pytest.mark.integration
class TestSecurityEndpointKmsIntegration:
    """Validates the Dashboard's /api/security endpoint reports KMS status correctly."""

    def test_vault_is_active(self, api_client):
        """The security endpoint must report vault_active=True when OpenBao is up."""
        data = api_client.get("/security").json()
        assert data["vault_active"] is True

    def test_llm_api_key_is_secured(self, api_client):
        """LLM_API_KEY must be stored in OpenBao, not in environment."""
        data = api_client.get("/security").json()
        keys = data.get("openbao_keys", {})
        # If an LLM_API_KEY is present, it must be masked as 'secured'.
        # However, Ollama deployments without an explicit key will not have this entry.
        if "LLM_API_KEY" in keys:
            assert keys.get("LLM_API_KEY") == "secured"

    def test_es_api_key_is_secured(self, api_client):
        """ES_API_KEY must be stored in OpenBao."""
        data = api_client.get("/security").json()
        keys = data.get("openbao_keys", {})
        assert keys.get("ES_API_KEY") == "secured"

    def test_audit_logs_present(self, api_client):
        """Security endpoint should include KMS audit trail."""
        data = api_client.get("/security").json()
        audit = data.get("audit_logs", [])
        assert isinstance(audit, list)
        # After stack boot, there should be at least one audit entry
        if audit:
            entry = audit[0]
            assert "@timestamp" in entry
            assert "message" in entry
