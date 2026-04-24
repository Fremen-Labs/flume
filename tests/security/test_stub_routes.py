"""
Security tests for stub routes.

Validates the behavior of known stub routes and ensures they do not
leak internal state, crash, or return unexpected status codes.
Stubs are explicitly marked with xfail to document their mock nature.

Requires: Flume stack running (./flume start)
"""
import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Stub Route Behavior — /api/tasks/claim
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.security
class TestStubTaskClaim:
    """POST /api/tasks/claim — Known stub returning mock data."""

    @pytest.mark.xfail(reason="Stub route — returns hardcoded mock, never touches ES")
    def test_claim_returns_real_task_id(self, api_client):
        """When implemented, should return a real ES doc ID, not 'mock_id'."""
        resp = api_client.post(
            "/tasks/claim",
            json={"worker_id": "test-worker-1"},
        )
        data = resp.json()
        assert data.get("task_id") != "mock_id", (
            "Still returning hardcoded mock_id — implementation pending"
        )

    def test_claim_does_not_crash(self, api_client):
        """Stub endpoint must not 500 even without a valid worker."""
        resp = api_client.post(
            "/tasks/claim",
            json={"worker_id": "test-worker"},
        )
        assert resp.status_code in (200, 422), (
            f"Stub claim route returned unexpected {resp.status_code}"
        )

    def test_claim_requires_worker_id(self, api_client):
        """Pydantic validation should enforce the worker_id field."""
        resp = api_client.post("/tasks/claim", json={})
        assert resp.status_code == 422  # Pydantic validation error


# ═══════════════════════════════════════════════════════════════════════════════
# Stub Route Behavior — /api/tasks/complete
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.security
class TestStubTaskComplete:
    """POST /api/tasks/complete — Known stub returning mock data."""

    @pytest.mark.xfail(reason="Stub route — returns hardcoded 'completed', never touches ES")
    def test_complete_performs_real_transition(self, api_client):
        """When implemented, should update the task's ES document status."""
        resp = api_client.post(
            "/tasks/complete",
            params={"task_id": "nonexistent-task-id"},
        )
        data = resp.json()
        # Once real, the status should reflect the actual transition
        assert "error" in data or data.get("status") != "completed"

    def test_complete_does_not_crash(self, api_client):
        """Stub endpoint must not 500."""
        resp = api_client.post(
            "/tasks/complete",
            params={"task_id": "any-task-id"},
        )
        assert resp.status_code != 500


# ═══════════════════════════════════════════════════════════════════════════════
# Stub Route Behavior — /api/settings/restart-services
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.security
class TestStubRestartServices:
    """POST /api/settings/restart-services — Known stub returning static OK."""

    @pytest.mark.xfail(reason="Stub route — returns static {success: true}, no actual restart")
    def test_restart_performs_real_action(self, api_client):
        """When implemented, should actually restart services and report results."""
        resp = api_client.post("/settings/restart-services")
        data = resp.json()
        # Once real, should contain details about what was restarted
        assert "restarted_services" in data

    def test_restart_does_not_crash(self, api_client):
        """Stub endpoint must not 500."""
        resp = api_client.post("/settings/restart-services")
        assert resp.status_code != 500

    def test_restart_returns_success_shape(self, api_client):
        """Even as a stub, should return a well-formed response."""
        resp = api_client.post("/settings/restart-services")
        if resp.status_code == 200:
            data = resp.json()
            assert "success" in data or "status" in data
