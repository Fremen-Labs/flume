import os
import shutil
import tempfile
import time
import subprocess
import pytest
import httpx

# Determine base API URL for the Dashboard Server
FLUME_API_BASE = os.environ.get("FLUME_API_BASE", "http://localhost:8765/api")

@pytest.fixture(scope="session")
def api_client():
    """Provides a synchronous HTTP client bound to the Flume Dashboard API"""
    with httpx.Client(base_url=FLUME_API_BASE) as client:
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
    Kubernetes Namespace Pattern Equivalent.
    Creates a highly isolated, throwaway Git repository in /tmp for the duration
    of the test to prevent polluting fremenlabs live git trees.
    Returns the absolute path to the generic repo.
    """
    tmp_path = tempfile.mkdtemp(prefix="flume-e2e-repo-")
    
    # Initialize basic git state so Flume Git routines accept it
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    
    # Create an initial commit so branch logic doesn't crash on empty tree
    readme_path = os.path.join(tmp_path, "README.md")
    with open(readme_path, "w") as f:
        f.write("# Flume E2E Mock Repo\n\nThis is an ephemeral test target.")
        
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmp_path, check=True, capture_output=True)
    
    yield tmp_path
    
    # Teardown the isolated namespace
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture
def isolated_flume_project(api_client, mock_git_repo):
    """
    Registers the `mock_git_repo` as a full Flume project and injects it into ES.
    Cleans up the project upon test completion.
    """
    project_id = os.path.basename(mock_git_repo)
    
    # Register purely local project
    payload = {
        "repoId": project_id,
        "path": mock_git_repo,
        "cloneStatus": "local"
    }
    
    # Depending on exact API signature in api.projects
    resp = api_client.post(f"/projects/{project_id}/register", json=payload)
    if resp.status_code == 404:
        # Fallback if route syntax differs
        resp = api_client.post("/projects", json={"path": mock_git_repo})
        
    yield project_id
    
    # Teardown: Remove project from Elasticsearch
    # (Assuming DELETE /api/projects/{id} exists)
    api_client.delete(f"/projects/{project_id}")

