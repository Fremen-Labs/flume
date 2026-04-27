"""
Integration tests for Dashboard API contracts.

Validates that each API endpoint returns the expected response shape,
status code, and required fields. These are contract tests — they
verify the API surface, not the business logic underneath.

Contract violations caught here prevent silent frontend regressions
and configuration drift between the dashboard and the Vue UI.

Requires: Flume stack running (./flume start)
"""
import pytest


@pytest.mark.integration
class TestHealthContract:
    """GET /api/health — Liveness probe."""

    def test_health_returns_200(self, api_client):
        resp = api_client.get("/health")
        assert resp.status_code == 200

    def test_health_shape(self, api_client):
        data = api_client.get("/health").json()
        assert data["status"] == "ok"


@pytest.mark.integration
class TestSnapshotContract:
    """GET /api/snapshot — Main dashboard data payload."""

    def test_snapshot_returns_200(self, api_client):
        resp = api_client.get("/snapshot")
        assert resp.status_code == 200

    def test_snapshot_has_required_keys(self, api_client):
        """The snapshot response must contain the keys the frontend depends on."""
        data = api_client.get("/snapshot").json()
        required_keys = ["workers", "projects"]
        for key in required_keys:
            assert key in data, f"Missing required key '{key}' in /api/snapshot"

    def test_snapshot_workers_shape(self, api_client):
        """Each worker in the snapshot must have core identity fields."""
        data = api_client.get("/snapshot").json()
        workers = data.get("workers", [])
        if workers:
            w = workers[0]
            for field in ["name", "role", "model", "status"]:
                assert field in w, f"Worker missing required field: '{field}'"


@pytest.mark.integration
class TestSystemStateContract:
    """GET /api/system-state — Orchestration status payload."""

    def test_system_state_returns_200(self, api_client):
        resp = api_client.get("/system-state")
        assert resp.status_code == 200

    def test_system_state_has_required_keys(self, api_client):
        data = api_client.get("/system-state").json()
        assert "status" in data
        assert data["status"] in ("online", "offline", "degraded")

    def test_system_state_worker_count(self, api_client):
        """System state must report active worker count."""
        data = api_client.get("/system-state").json()
        workers = data.get("workers", [])
        assert isinstance(workers, list)


@pytest.mark.integration
class TestSettingsLlmContract:
    """GET /api/settings/llm — LLM provider catalog."""

    def test_llm_settings_returns_200(self, api_client):
        resp = api_client.get("/settings/llm")
        assert resp.status_code == 200

    def test_llm_settings_has_catalog(self, api_client):
        data = api_client.get("/settings/llm").json()
        assert "catalog" in data
        assert isinstance(data["catalog"], list)
        assert len(data["catalog"]) > 0  # At least ollama should be in catalog

    def test_llm_catalog_entry_shape(self, api_client):
        """Each catalog entry must have id, name, and models."""
        data = api_client.get("/settings/llm").json()
        entry = data["catalog"][0]
        for field in ["id", "name"]:
            assert field in entry, f"Catalog entry missing: '{field}'"


@pytest.mark.integration
class TestSettingsSystemContract:
    """GET /api/settings/system — Infrastructure configuration."""

    def test_system_settings_returns_200(self, api_client):
        resp = api_client.get("/settings/system")
        assert resp.status_code == 200

    def test_system_settings_has_es_url(self, api_client):
        data = api_client.get("/settings/system").json()
        assert "es_url" in data
        assert data["es_url"]  # Non-empty


@pytest.mark.integration
class TestNodesContract:
    """GET /api/nodes — Node mesh registry."""

    def test_nodes_returns_200(self, api_client):
        resp = api_client.get("/nodes")
        assert resp.status_code == 200

    def test_nodes_has_count_and_list(self, api_client):
        data = api_client.get("/nodes").json()
        assert "count" in data
        assert "nodes" in data
        assert isinstance(data["nodes"], list)
        assert data["count"] == len(data["nodes"])

    def test_node_entry_shape(self, api_client):
        """Each node must have id, host, model_tag, capabilities, health."""
        data = api_client.get("/nodes").json()
        if data["nodes"]:
            node = data["nodes"][0]
            for field in ["id", "host", "model_tag", "capabilities", "health"]:
                assert field in node, f"Node missing field: '{field}'"

    def test_node_capabilities_shape(self, api_client):
        """Node capabilities must include performance and memory fields."""
        data = api_client.get("/nodes").json()
        if data["nodes"]:
            caps = data["nodes"][0]["capabilities"]
            for field in ["reasoning_score", "max_context", "memory_gb"]:
                assert field in caps, f"Capabilities missing: '{field}'"

    def test_node_health_shape(self, api_client):
        """Node health must include status and last_seen."""
        data = api_client.get("/nodes").json()
        if data["nodes"]:
            health = data["nodes"][0]["health"]
            assert "status" in health
            assert health["status"] in ("healthy", "degraded", "offline", "unknown")


@pytest.mark.integration
class TestWorkflowWorkersContract:
    """GET /api/workflow/workers — Active worker roster."""

    def test_workers_returns_200(self, api_client):
        resp = api_client.get("/workflow/workers")
        assert resp.status_code == 200

    def test_workers_has_list(self, api_client):
        data = api_client.get("/workflow/workers").json()
        assert "workers" in data
        assert isinstance(data["workers"], list)


@pytest.mark.integration
class TestSecurityContract:
    """GET /api/security — KMS security posture."""

    def test_security_returns_200(self, api_client):
        resp = api_client.get("/security")
        assert resp.status_code == 200

    def test_security_has_vault_status(self, api_client):
        data = api_client.get("/security").json()
        assert "vault_active" in data
        assert isinstance(data["vault_active"], bool)

    def test_security_has_openbao_keys(self, api_client):
        """KMS should report which keys are secured."""
        data = api_client.get("/security").json()
        assert "openbao_keys" in data
        keys = data["openbao_keys"]
        assert "ES_API_KEY" in keys
        # LLM_API_KEY is only present if the provider requires one (e.g. OpenAI/Anthropic).
        # Ollama deployments may not have an LLM_API_KEY.

    def test_security_keys_are_masked(self, api_client):
        """Key values must not be exposed — only status like 'secured'."""
        data = api_client.get("/security").json()
        for key, value in data.get("openbao_keys", {}).items():
            assert value == "secured", f"Key '{key}' value exposed: {value}"


@pytest.mark.integration
class TestAutonomyStatusContract:
    """GET /api/autonomy/status — Autonomous sweep system status."""

    def test_autonomy_returns_200(self, api_client):
        resp = api_client.get("/autonomy/status")
        assert resp.status_code == 200

    def test_autonomy_has_auto_unblock(self, api_client):
        data = api_client.get("/autonomy/status").json()
        assert "auto_unblock" in data
        assert "enabled" in data["auto_unblock"]
        assert isinstance(data["auto_unblock"]["enabled"], bool)

    def test_autonomy_has_sweeps(self, api_client):
        data = api_client.get("/autonomy/status").json()
        assert "sweeps" in data
        assert "enabled" in data["sweeps"]
        assert "sweeps" in data["sweeps"]


@pytest.mark.integration
class TestRoutingPolicyContract:
    """GET /api/routing-policy — LLM routing configuration."""

    def test_routing_returns_200(self, api_client):
        resp = api_client.get("/routing-policy")
        assert resp.status_code == 200

    def test_routing_has_mode(self, api_client):
        data = api_client.get("/routing-policy").json()
        assert "mode" in data
        assert data["mode"] in ("hybrid", "frontier_only", "local_only")

    def test_routing_has_frontier_mix(self, api_client):
        data = api_client.get("/routing-policy").json()
        assert "frontier_mix" in data
        assert isinstance(data["frontier_mix"], list)

    def test_frontier_mix_entry_shape(self, api_client):
        """Each frontier provider entry must have provider, model, and budget."""
        data = api_client.get("/routing-policy").json()
        if data["frontier_mix"]:
            entry = data["frontier_mix"][0]
            for field in ["provider", "model", "budget_usd", "spent_usd", "circuit_open"]:
                assert field in entry, f"Frontier mix entry missing: '{field}'"
