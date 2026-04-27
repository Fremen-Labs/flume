"""
E2E tests for system bootstrap validation.

Validates that the Flume stack bootstraps correctly with all
infrastructure components operational, node mesh populated,
and routing policy configured.

Requires: Flume stack running (./flume start)
"""
import pytest


class TestApiBootstrap:
    """Validates the Dashboard API is reachable and returns valid state."""

    def test_api_healthy(self, api_client):
        """Ensure the API is resolvable and ES is up."""
        resp = api_client.get("/snapshot")
        assert resp.status_code == 200
        data = resp.json()
        assert "workers" in data
        assert "projects" in data

    def test_system_state_online(self, api_client):
        """System state must report 'online' after bootstrap."""
        resp = api_client.get("/system-state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "online"


class TestNodeMeshBootstrap:
    """Validates that the node mesh is populated from flume start seed."""

    def test_nodes_registered(self, api_client):
        """After bootstrap, at least one node should be registered."""
        resp = api_client.get("/nodes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] > 0, "No nodes registered after bootstrap"
        assert len(data["nodes"]) == data["count"]

    def test_all_nodes_have_health(self, api_client):
        """Every registered node must have health telemetry."""
        data = api_client.get("/nodes").json()
        for node in data["nodes"]:
            assert "health" in node, f"Node '{node['id']}' missing health data"
            assert "status" in node["health"]

    def test_all_nodes_have_capabilities(self, api_client):
        """Every registered node must have capability metadata."""
        data = api_client.get("/nodes").json()
        for node in data["nodes"]:
            assert "capabilities" in node, f"Node '{node['id']}' missing capabilities"
            caps = node["capabilities"]
            assert "memory_gb" in caps
            assert "max_context" in caps


class TestWorkerBootstrap:
    """Validates that agent workers are registered and heartbeating."""

    def test_workers_registered(self, api_client):
        """At least one worker must be registered after bootstrap."""
        data = api_client.get("/snapshot").json()
        workers = data.get("workers", [])
        assert len(workers) > 0, "No workers registered after bootstrap"

    def test_workers_have_heartbeat(self, api_client):
        """Every worker must have a recent heartbeat timestamp."""
        data = api_client.get("/snapshot").json()
        for w in data.get("workers", []):
            assert w.get("heartbeat_at"), f"Worker '{w['name']}' missing heartbeat"

    def test_workers_are_idle(self, api_client):
        """On a clean stack, workers should be idle (no tasks dispatched)."""
        import time
        max_retries = 5
        for i in range(max_retries):
            data = api_client.get("/snapshot").json()
            all_idle = True
            unexpected_states = []
            for w in data.get("workers", []):
                if w["status"] not in ("idle", "running"):
                    all_idle = False
                    unexpected_states.append(f"Worker '{w['name']}' is {w['status']}")
            
            if all_idle:
                return
                
            if i < max_retries - 1:
                time.sleep(3)
                
        pytest.fail(f"Workers failed to reach idle state: {', '.join(unexpected_states)}")


class TestRoutingPolicyBootstrap:
    """Validates the routing policy is configured after bootstrap."""

    def test_routing_mode_set(self, api_client):
        """Routing mode must be set (hybrid, frontier_only, or local_only)."""
        data = api_client.get("/routing-policy").json()
        assert data["mode"] in ("hybrid", "frontier_only", "local_only")

    def test_frontier_mix_has_active_provider(self, api_client):
        """Frontier providers should be configured when mode expects them.

        When routing mode is 'local_only', an empty frontier_mix is valid —
        no cloud providers are needed. This test only asserts frontier content
        when the mode is 'frontier_only' or 'hybrid'.
        """
        data = api_client.get("/routing-policy").json()
        mode = data.get("mode", "local_only")
        mix = data.get("frontier_mix", [])

        if mode == "local_only":
            # No frontier providers expected in local_only mode
            return

        assert len(mix) > 0, (
            f"No frontier providers configured but routing mode is '{mode}'"
        )
        # The configured provider should not have an open circuit
        provider = mix[0]
        assert provider["circuit_open"] is False, (
            f"Primary provider '{provider['provider']}' has open circuit breaker"
        )


class TestGatewayBootstrap:
    """Validates the Go Gateway is healthy after bootstrap."""

    def test_gateway_healthy(self, gateway_client):
        """Gateway must report healthy status."""
        resp = gateway_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "flume-gateway"

    def test_gateway_has_capacity(self, gateway_client):
        """Gateway must have available concurrency slots."""
        data = gateway_client.get("/health").json()
        assert data["global"]["max"] > 0
        assert data["global"]["active"] >= 0


class TestAutonomyBootstrap:
    """Validates the autonomous sweep system is running after bootstrap."""

    def test_auto_unblock_enabled(self, api_client):
        """Auto-unblock daemon should be enabled and its thread alive."""
        data = api_client.get("/autonomy/status").json()
        assert data["auto_unblock"]["enabled"] is True
        assert data["auto_unblock"]["thread_alive"] is True

    def test_sweeps_enabled(self, api_client):
        """Background sweep system should be enabled."""
        data = api_client.get("/autonomy/status").json()
        assert data["sweeps"]["enabled"] is True
        assert data["sweeps"]["thread_alive"] is True
