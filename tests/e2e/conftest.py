import os
import shutil
import tempfile
import subprocess
import pytest
import httpx

# Determine base API URL for the Dashboard Server
FLUME_API_BASE = os.environ.get("FLUME_API_BASE", "http://localhost:8765/api")
FLUME_GATEWAY_URL = os.environ.get("FLUME_GATEWAY_URL", "http://localhost:8090")

@pytest.fixture(scope="session")
def api_client():
    """Provides a synchronous HTTP client bound to the Flume Dashboard API."""
    with httpx.Client(base_url=FLUME_API_BASE, timeout=30.0) as client:
        yield client

@pytest.fixture(scope="session")
def gateway_client():
    """Provides a synchronous HTTP client bound to the Go Gateway."""
    with httpx.Client(base_url=FLUME_GATEWAY_URL, timeout=10.0) as client:
        yield client

@pytest.fixture
def flume_waiter(api_client):
    from .waiters import FlumeWaiter
    return FlumeWaiter(api_client)

@pytest.fixture(scope="session")
def real_ado_repo():
    """Provides the live ADO URL for E2E branch testing"""
    return "https://mentat-automation@dev.azure.com/mentat-automation/fremenlabs/_git/elastro-website"

@pytest.fixture
def mock_git_repo():
    """
    Creates an isolated throwaway Git repository with a single initial commit.
    Returns the absolute path; cleans up on teardown.
    """
    tmp_path = tempfile.mkdtemp(prefix="flume-e2e-repo-")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    readme_path = os.path.join(tmp_path, "README.md")
    with open(readme_path, "w") as f:
        f.write("# Flume E2E Mock Repo\n\nThis is an ephemeral test target.")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmp_path, check=True, capture_output=True)
    yield tmp_path
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture
def project_with_code():
    """
    Creates a multi-file mock repository with Python code, config, and README.
    Simulates a realistic project structure for code browsing and planning tests.
    """
    tmp_path = tempfile.mkdtemp(prefix="flume-e2e-code-")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)

    # README.md
    with open(os.path.join(tmp_path, "README.md"), "w") as f:
        f.write("# E2E Test Project\n\nA realistic multi-file project for testing.\n")

    # Python source
    src_dir = os.path.join(tmp_path, "src")
    os.makedirs(src_dir)
    with open(os.path.join(src_dir, "main.py"), "w") as f:
        f.write('"""Main entry point."""\n\ndef hello():\n    return "Hello from Flume E2E test"\n\nif __name__ == "__main__":\n    print(hello())\n')
    with open(os.path.join(src_dir, "utils.py"), "w") as f:
        f.write('"""Utility functions."""\n\ndef add(a: int, b: int) -> int:\n    return a + b\n')

    # Config files
    with open(os.path.join(tmp_path, "pyproject.toml"), "w") as f:
        f.write('[project]\nname = "e2e-test-project"\nversion = "0.1.0"\n')
    with open(os.path.join(tmp_path, ".gitignore"), "w") as f:
        f.write("__pycache__/\n*.pyc\n.venv/\n")

    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial multi-file commit"], cwd=tmp_path, check=True, capture_output=True)
    yield tmp_path
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture
def isolated_flume_project(api_client, mock_git_repo):
    """
    Registers the `mock_git_repo` as a Flume project.
    Yields the project_id; cleans up on teardown.
    """
    project_id = os.path.basename(mock_git_repo)
    payload = {
        "name": f"test-repo-{project_id}",
        "localPath": mock_git_repo
    }
    resp = api_client.post("/projects", json=payload)
    assert resp.status_code == 200, (
        f"Failed to create isolated project: {resp.status_code} — {resp.text}"
    )
    data = resp.json()
    new_project_id = data.get("projectId", project_id)
    yield new_project_id

    # Teardown: Remove project from Elasticsearch
    if new_project_id:
        api_client.post(f"/projects/{new_project_id}/delete")


@pytest.fixture
def code_project(api_client, project_with_code):
    """
    Registers the `project_with_code` multi-file repo as a Flume project.
    Yields the project_id; cleans up on teardown.
    """
    project_id = os.path.basename(project_with_code)
    payload = {
        "name": f"code-project-{project_id}",
        "localPath": project_with_code
    }
    resp = api_client.post("/projects", json=payload)
    assert resp.status_code == 200, (
        f"Failed to create code project: {resp.status_code} — {resp.text}"
    )
    data = resp.json()
    new_project_id = data.get("projectId", project_id)
    yield new_project_id

    if new_project_id:
        api_client.post(f"/projects/{new_project_id}/delete")
