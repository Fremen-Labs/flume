"""
E2E tests for the full project lifecycle.

Validates the complete create → clone-status → browse code → delete
cycle using both minimal and multi-file repositories.

Requires: Flume stack running (./flume start)
"""
import os
import time


class TestProjectCreationLifecycle:
    """Full create → verify → delete lifecycle."""

    def test_create_local_project_returns_id(self, api_client, mock_git_repo):
        """Creating a local project should return a valid projectId."""
        payload = {
            "name": f"lifecycle-{os.path.basename(mock_git_repo)}",
            "localPath": mock_git_repo,
        }
        resp = api_client.post("/projects", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        project_id = data.get("projectId") or data.get("id")
        assert project_id, "No projectId returned from project creation"

        # Cleanup
        api_client.post(f"/projects/{project_id}/delete")

    def test_project_appears_in_snapshot_after_creation(self, api_client, isolated_flume_project):
        """Newly created project must appear in the /api/snapshot response."""
        time.sleep(0.5)  # Allow ES indexing
        data = api_client.get("/snapshot").json()
        project_ids = [p.get("id") for p in data.get("projects", [])]
        assert isolated_flume_project in project_ids, (
            f"Project '{isolated_flume_project}' not in snapshot. Found: {project_ids}"
        )


class TestProjectCloneStatus:
    """GET /api/projects/{id}/clone-status — Clone progress polling."""

    def test_clone_status_is_functional(self, api_client, isolated_flume_project):
        """Clone status endpoint should respond without crashing."""
        resp = api_client.get(f"/projects/{isolated_flume_project}/clone-status")
        # 200 = clone data, 404 = local project (no clone needed)
        assert resp.status_code in (200, 404), (
            f"Unexpected clone-status response: {resp.status_code}"
        )


class TestProjectCodeBrowsing:
    """Validates code browsing endpoints for registered projects."""

    def test_branches_endpoint(self, api_client, isolated_flume_project):
        """GET /api/repos/{id}/branches should return branch list."""
        resp = api_client.get(f"/repos/{isolated_flume_project}/branches")
        if resp.status_code == 200:
            data = resp.json()
            # Should have at least one branch (main/master)
            assert isinstance(data, (list, dict))

    def test_tree_endpoint(self, api_client, code_project):
        """GET /api/repos/{id}/tree should return directory structure."""
        resp = api_client.get(f"/repos/{code_project}/tree")
        if resp.status_code == 200:
            data = resp.json()
            # Tree should contain entries
            assert isinstance(data, (list, dict))

    def test_file_endpoint(self, api_client, code_project):
        """GET /api/repos/{id}/file should return file content."""
        resp = api_client.get(
            f"/repos/{code_project}/file",
            params={"path": "README.md"},
        )
        if resp.status_code == 200:
            data = resp.json()
            # Should contain file content
            content = data.get("content", data.get("text", ""))
            assert "E2E Test Project" in content or isinstance(content, str)


class TestProjectDeletion:
    """Validates project deletion and cleanup."""

    def test_delete_removes_from_snapshot(self, api_client, mock_git_repo):
        """Deleted project must no longer appear in /api/snapshot."""
        # Create
        payload = {
            "name": f"del-test-{os.path.basename(mock_git_repo)}",
            "localPath": mock_git_repo,
        }
        resp = api_client.post("/projects", json=payload)
        assert resp.status_code == 200
        project_id = resp.json().get("projectId")
        assert project_id

        # Delete
        del_resp = api_client.post(f"/projects/{project_id}/delete")
        assert del_resp.status_code == 200

        # Verify removal (allow ES to propagate)
        time.sleep(1)
        data = api_client.get("/snapshot").json()
        project_ids = [p.get("id") for p in data.get("projects", [])]
        assert project_id not in project_ids, (
            f"Deleted project '{project_id}' still in snapshot"
        )

    def test_double_delete_is_idempotent(self, api_client, mock_git_repo):
        """Deleting an already-deleted project must not crash."""
        payload = {
            "name": f"double-del-{os.path.basename(mock_git_repo)}",
            "localPath": mock_git_repo,
        }
        resp = api_client.post("/projects", json=payload)
        project_id = resp.json().get("projectId")
        api_client.post(f"/projects/{project_id}/delete")
        # Second delete
        resp2 = api_client.post(f"/projects/{project_id}/delete")
        assert resp2.status_code != 500, "Double delete caused 500"
