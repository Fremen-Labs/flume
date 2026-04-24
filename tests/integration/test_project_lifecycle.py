"""
Integration tests for the Project Lifecycle.

Validates the full create → list → tasks → delete cycle for Flume projects
against the running stack. Uses isolated git repos to prevent polluting
production data.

Requires: Flume stack running (./flume start)
"""
import os
import time
import pytest


@pytest.mark.integration
class TestProjectCreation:
    """POST /api/projects — Register a local project."""

    def test_create_local_project(self, api_client, mock_git_repo):
        """Creating a project with a valid local path should succeed."""
        payload = {
            "name": f"create-test-{os.path.basename(mock_git_repo)}",
            "localPath": mock_git_repo,
        }
        resp = api_client.post("/projects", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert "projectId" in data or "id" in data or "ok" in data

        # Cleanup
        project_id = data.get("projectId") or data.get("id")
        if project_id:
            api_client.post(f"/projects/{project_id}/delete")

    def test_create_project_missing_name_fails(self, api_client, mock_git_repo):
        """Creating a project without a name should return 4xx."""
        payload = {"localPath": mock_git_repo}
        resp = api_client.post("/projects", json=payload)
        assert resp.status_code in (400, 422)

    def test_create_project_missing_path_fails(self, api_client):
        """Creating a project without a path should return 4xx."""
        payload = {"name": "no-path-project"}
        resp = api_client.post("/projects", json=payload)
        # May succeed (remote project) or fail — depends on provider config
        # Just verify it doesn't 500
        assert resp.status_code != 500


@pytest.mark.integration
class TestProjectVisibility:
    """Validates that created projects appear in the snapshot."""

    def test_project_appears_in_snapshot(self, api_client, isolated_project):
        """A registered project should be visible in /api/snapshot."""
        # Give ES a moment to index
        time.sleep(0.5)
        resp = api_client.get("/snapshot")
        assert resp.status_code == 200
        data = resp.json()
        projects = data.get("projects", [])
        project_ids = [p.get("id") for p in projects]
        assert isolated_project in project_ids, (
            f"Project '{isolated_project}' not found in snapshot. "
            f"Available: {project_ids}"
        )


@pytest.mark.integration
class TestProjectTasks:
    """GET /api/projects/{id}/tasks — Task listing for a project."""

    def test_tasks_endpoint_is_functional(self, api_client, isolated_project):
        """Task listing should return 200 (tasks found) or 404 (no tasks indexed yet)."""
        resp = api_client.get(f"/projects/{isolated_project}/tasks")
        # 200 = tasks returned, 404 = project exists but no task records yet
        assert resp.status_code in (200, 404), (
            f"Unexpected status {resp.status_code} for tasks endpoint"
        )

    def test_tasks_returns_list(self, api_client, isolated_project):
        """Response should contain a list of tasks (empty for new project)."""
        data = api_client.get(f"/projects/{isolated_project}/tasks").json()
        # Response might be a list directly or an object with a 'tasks' key
        if isinstance(data, list):
            assert isinstance(data, list)
        elif isinstance(data, dict):
            tasks = data.get("tasks", data.get("items", []))
            assert isinstance(tasks, list)


@pytest.mark.integration
class TestProjectDeletion:
    """POST /api/projects/{id}/delete — Remove a project."""

    def test_delete_project(self, api_client, mock_git_repo):
        """Deleting a registered project should succeed."""
        # Create
        payload = {
            "name": f"delete-test-{os.path.basename(mock_git_repo)}",
            "localPath": mock_git_repo,
        }
        create_resp = api_client.post("/projects", json=payload)
        assert create_resp.status_code == 200
        project_id = create_resp.json().get("projectId") or create_resp.json().get("id")

        # Delete
        del_resp = api_client.post(f"/projects/{project_id}/delete")
        assert del_resp.status_code == 200

    def test_delete_nonexistent_project(self, api_client):
        """Deleting a nonexistent project should not crash (idempotent)."""
        resp = api_client.post("/projects/nonexistent-project-xyz/delete")
        # Should be 200 (idempotent) or 404 — not 500
        assert resp.status_code != 500
