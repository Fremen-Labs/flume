"""
Shared integration test configuration.

All integration tests assume the Flume stack is running:
  - Dashboard: http://localhost:8765
  - Elasticsearch: https://localhost:9200 (TLS + Basic Auth)
  - Gateway: http://localhost:8090
  - OpenBao: http://localhost:8200
"""
import os
import shutil
import tempfile
import subprocess
import pytest
import httpx

# ── DRY imports from root conftest ───────────────────────────────────────────
from tests.conftest import (
    FLUME_API_BASE,
    FLUME_ES_URL,
    FLUME_GATEWAY_URL,
    FLUME_OPENBAO_URL,
    get_elastic_password,
    make_es_client,
)

FLUME_ES_PASSWORD = get_elastic_password()


@pytest.fixture(scope="session")
def api_client():
    """Session-scoped HTTP client bound to the Flume Dashboard API."""
    with httpx.Client(base_url=FLUME_API_BASE, timeout=15.0) as client:
        yield client


@pytest.fixture(scope="session")
def es_client():
    """Session-scoped HTTP client bound to the Elasticsearch instance.

    Uses HTTPS with self-signed cert verification disabled (verify=False)
    and Basic Auth via the FLUME_ELASTIC_PASSWORD env var, matching the
    TLS-enabled docker-compose configuration.
    """
    client = make_es_client(password=FLUME_ES_PASSWORD)
    yield client
    client.close()


@pytest.fixture(scope="session")
def gateway_client():
    """Session-scoped HTTP client bound to the Go Gateway."""
    with httpx.Client(base_url=FLUME_GATEWAY_URL, timeout=10.0) as client:
        yield client


@pytest.fixture(scope="session")
def openbao_client():
    """Session-scoped HTTP client bound to the OpenBao KMS."""
    with httpx.Client(base_url=FLUME_OPENBAO_URL, timeout=10.0) as client:
        yield client


@pytest.fixture
def mock_git_repo():
    """
    Creates an isolated throwaway Git repository for integration tests.
    Returns the absolute path; cleans up on teardown.
    """
    tmp_path = tempfile.mkdtemp(prefix="flume-int-repo-")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    readme_path = os.path.join(tmp_path, "README.md")
    with open(readme_path, "w") as f:
        f.write("# Flume Integration Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    yield tmp_path
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture
def isolated_project(api_client, mock_git_repo):
    """
    Registers a local git repo as a Flume project, yields the project_id,
    and deletes the project on teardown.
    """
    project_id = os.path.basename(mock_git_repo)
    payload = {"name": f"int-test-{project_id}", "localPath": mock_git_repo}
    resp = api_client.post("/projects", json=payload)
    if resp.status_code == 200:
        data = resp.json()
        new_id = data.get("projectId", project_id)
    else:
        new_id = project_id
    yield new_id
    # Teardown
    api_client.post(f"/projects/{new_id}/delete")
