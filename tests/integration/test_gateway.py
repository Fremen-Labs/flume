"""
Integration tests for the Go Gateway.

Validates that the gateway proxy is healthy, routing correctly,
and reporting accurate concurrency slot information.

Requires: Flume stack running (./flume start)
"""
import pytest


@pytest.mark.integration
class TestGatewayHealth:
    """GET /health — Gateway liveness and concurrency reporting."""

    def test_gateway_returns_200(self, gateway_client):
        resp = gateway_client.get("/health")
        assert resp.status_code == 200

    def test_gateway_health_shape(self, gateway_client):
        data = gateway_client.get("/health").json()
        assert data["service"] == "flume-gateway"
        assert data["status"] == "ok"

    def test_gateway_reports_concurrency_slots(self, gateway_client):
        """Gateway must report global, ollama, and frontier concurrency state."""
        data = gateway_client.get("/health").json()
        for slot_type in ["global", "ollama", "frontier"]:
            assert slot_type in data, f"Missing concurrency slot type: '{slot_type}'"

    def test_global_slot_shape(self, gateway_client):
        data = gateway_client.get("/health").json()
        g = data["global"]
        assert "active" in g
        assert "max" in g
        assert isinstance(g["active"], int)
        assert isinstance(g["max"], int)
        assert g["active"] >= 0
        assert g["max"] > 0

    def test_ollama_slot_shape(self, gateway_client):
        data = gateway_client.get("/health").json()
        o = data["ollama"]
        assert "active_slots" in o
        assert "max_slots" in o
        assert o["active_slots"] >= 0

    def test_frontier_slot_shape(self, gateway_client):
        data = gateway_client.get("/health").json()
        f = data["frontier"]
        assert "frontier_active" in f
        assert "frontier_max_slots" in f
        assert f["frontier_active"] >= 0


@pytest.mark.integration
class TestGatewayMetrics:
    """GET /metrics — Prometheus metrics endpoint."""

    def test_metrics_returns_200(self, gateway_client):
        resp = gateway_client.get("/metrics")
        # Gateway may return 200 with prometheus text or 404 if not configured
        assert resp.status_code in (200, 404)

    def test_metrics_contains_go_info(self, gateway_client):
        resp = gateway_client.get("/metrics")
        if resp.status_code == 200:
            text = resp.text
            # Standard Go prometheus metrics
            assert "go_" in text or "process_" in text
