"""
Security tests for admin authorization boundaries.

Validates that all admin-gated endpoints correctly enforce Bearer token
authentication and reject unauthorized requests with 403 Forbidden.

Tests cover the kill switch endpoints (stop-all, resume-all) and the
sync-ast system endpoint, which uses the X-Flume-System-Token header.

Follows OWASP Broken Access Control (A1:2017) testing guidance.

Requires: Flume stack running (./flume start)
"""
import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Kill Switch Auth Boundary — /api/tasks/stop-all
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.security
class TestStopAllAuthBoundary:
    """POST /api/tasks/stop-all — Must require valid admin Bearer token."""

    def test_rejects_no_auth_header(self, api_client):
        """Request with no Authorization header must return 403."""
        resp = api_client.post("/tasks/stop-all")
        assert resp.status_code == 403

    def test_rejects_empty_bearer_token(self, api_client):
        """Request with a trivially short/invalid token must return 403."""
        resp = api_client.post(
            "/tasks/stop-all",
            headers={"Authorization": "Bearer x"},
        )
        assert resp.status_code == 403

    def test_rejects_wrong_bearer_token(self, api_client):
        """Request with incorrect token must return 403."""
        resp = api_client.post(
            "/tasks/stop-all",
            headers={"Authorization": "Bearer wrong-token-12345"},
        )
        assert resp.status_code == 403

    def test_rejects_basic_auth(self, api_client):
        """Basic auth is not valid for admin endpoints — must return 403."""
        resp = api_client.post(
            "/tasks/stop-all",
            headers={"Authorization": "Basic YWRtaW46cGFzc3dvcmQ="},
        )
        assert resp.status_code == 403

    def test_rejection_body_contains_detail(self, api_client):
        """403 response must include a descriptive detail message."""
        resp = api_client.post("/tasks/stop-all")
        data = resp.json()
        assert "detail" in data
        assert "admin" in data["detail"].lower() or "access" in data["detail"].lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Kill Switch Auth Boundary — /api/tasks/resume-all
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.security
class TestResumeAllAuthBoundary:
    """POST /api/tasks/resume-all — Must require valid admin Bearer token."""

    def test_rejects_no_auth_header(self, api_client):
        """Request with no Authorization header must return 403."""
        resp = api_client.post("/tasks/resume-all")
        assert resp.status_code == 403

    def test_rejects_wrong_bearer_token(self, api_client):
        """Request with incorrect token must return 403."""
        resp = api_client.post(
            "/tasks/resume-all",
            headers={"Authorization": "Bearer wrong-token-12345"},
        )
        assert resp.status_code == 403

    def test_rejection_body_is_json(self, api_client):
        """403 response must be valid JSON with detail key."""
        resp = api_client.post("/tasks/resume-all")
        assert resp.headers.get("content-type", "").startswith("application/json")
        data = resp.json()
        assert "detail" in data


# ═══════════════════════════════════════════════════════════════════════════════
# Sync-AST Auth Boundary — /api/system/sync-ast
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.security
class TestSyncAstAuthBoundary:
    """POST /api/system/sync-ast — Uses X-Flume-System-Token header."""

    def test_rejects_no_token(self, api_client):
        """Request without X-Flume-System-Token must return 403."""
        resp = api_client.post("/system/sync-ast")
        assert resp.status_code == 403

    def test_rejects_wrong_token(self, api_client):
        """Request with incorrect system token must return 403."""
        resp = api_client.post(
            "/system/sync-ast",
            headers={"X-Flume-System-Token": "wrong-system-token"},
        )
        assert resp.status_code == 403

    def test_rejection_body_contains_forbidden_message(self, api_client):
        """403 response must contain the 'Forbidden' detail message."""
        resp = api_client.post("/system/sync-ast")
        data = resp.json()
        assert "detail" in data
        assert "forbidden" in data["detail"].lower() or "enforced" in data["detail"].lower()

    def test_bearer_token_does_not_work(self, api_client):
        """sync-ast uses X-Flume-System-Token, NOT Bearer auth — verify mismatch fails."""
        resp = api_client.post(
            "/system/sync-ast",
            headers={"Authorization": "Bearer some-token"},
        )
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════════
# Non-Admin Endpoints — Must NOT require auth
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.security
class TestPublicEndpointsAccessible:
    """Validates that read-only public endpoints do NOT require auth."""

    @pytest.mark.parametrize("path", [
        "/health",
        "/snapshot",
        "/system-state",
        "/settings/llm",
        "/settings/system",
        "/nodes",
        "/security",
        "/autonomy/status",
        "/routing-policy",
    ])
    def test_public_endpoint_no_auth_required(self, api_client, path):
        """Public GET endpoints must return 200 without any auth header."""
        resp = api_client.get(path)
        assert resp.status_code == 200, (
            f"Public endpoint {path} returned {resp.status_code} — expected 200"
        )
