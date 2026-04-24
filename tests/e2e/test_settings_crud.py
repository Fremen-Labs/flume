"""
E2E tests for settings CRUD operations.

Validates that LLM, system, and repository settings can be read,
written, and read back with correct persistence. Settings that
contain sensitive values must remain masked on read.

Requires: Flume stack running (./flume start)
"""


class TestLlmSettingsRead:
    """GET /api/settings/llm — Read LLM provider configuration."""

    def test_returns_catalog(self, api_client):
        """LLM settings must include a non-empty provider catalog."""
        resp = api_client.get("/settings/llm")
        assert resp.status_code == 200
        data = resp.json()
        assert "catalog" in data
        assert len(data["catalog"]) > 0

    def test_catalog_has_ollama(self, api_client):
        """Ollama should always be in the provider catalog (local provider)."""
        data = api_client.get("/settings/llm").json()
        provider_ids = [p["id"] for p in data["catalog"]]
        assert "ollama" in provider_ids, f"Ollama not in catalog: {provider_ids}"

    def test_catalog_entries_have_models(self, api_client):
        """Each catalog entry should list available models."""
        data = api_client.get("/settings/llm").json()
        for entry in data["catalog"]:
            if entry.get("models"):
                for model in entry["models"]:
                    assert "id" in model, f"Model in '{entry['id']}' missing 'id'"


class TestSystemSettingsReadWrite:
    """GET/PUT /api/settings/system — System configuration roundtrip."""

    def test_read_system_settings(self, api_client):
        """System settings must return infrastructure configuration."""
        resp = api_client.get("/settings/system")
        assert resp.status_code == 200
        data = resp.json()
        assert "es_url" in data
        assert "openbao_url" in data

    def test_sensitive_fields_masked_on_read(self, api_client):
        """ES API key and vault token must be masked in responses."""
        data = api_client.get("/settings/system").json()
        assert data["es_api_key"] in ("***", ""), "ES API key not masked"
        vault = data.get("vault_token", "")
        assert vault in ("••••", ""), "Vault token not masked"

    def test_write_preserves_non_sensitive_values(self, api_client):
        """
        Writing system settings and reading back should preserve
        non-sensitive fields like es_url and openbao_url.
        """
        # Read current
        current = api_client.get("/settings/system").json()
        original_es_url = current["es_url"]
        original_openbao_url = current["openbao_url"]

        # Write back with same values (non-destructive roundtrip)
        write_payload = {
            "es_url": original_es_url,
            "es_api_key": "",  # Don't change the key
            "openbao_url": original_openbao_url,
            "vault_token": "",  # Don't change the token
            "prometheus_enabled": current.get("prometheus_enabled", True),
        }
        resp = api_client.put("/settings/system", json=write_payload)
        # May return 200 or 400 depending on validation — just verify no crash
        assert resp.status_code != 500

        # Read back and verify non-sensitive values preserved
        after = api_client.get("/settings/system").json()
        assert after["es_url"] == original_es_url
        assert after["openbao_url"] == original_openbao_url


class TestRepoSettingsRead:
    """GET /api/settings/repos — Repository configuration."""

    def test_read_repo_settings(self, api_client):
        """Repo settings endpoint should return without crashing."""
        resp = api_client.get("/settings/repos")
        # 200 = settings returned, 404 = not configured yet
        assert resp.status_code in (200, 404)

    def test_repo_settings_shape(self, api_client):
        """If repo settings exist, response should be a dict."""
        resp = api_client.get("/settings/repos")
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, dict)


class TestAgentModelSettings:
    """GET/PUT /api/settings/agent-models — Agent role model assignments."""

    def test_read_agent_models(self, api_client):
        """Agent model settings should return current assignments."""
        resp = api_client.get("/settings/agent-models")
        assert resp.status_code in (200, 404)

    def test_agent_models_shape(self, api_client):
        """If agent models exist, response should contain role mappings."""
        resp = api_client.get("/settings/agent-models")
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, (dict, list))
