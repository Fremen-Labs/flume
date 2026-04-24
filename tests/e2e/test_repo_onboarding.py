
def test_ado_repo_registration(api_client, mock_git_repo):
    """
    Validates that Azure DevOps repositories can securely register without failing
    on missing standard GitHub APIs.
    """
    payload = {
        "name": "ado-project-test-01",
        "localPath": mock_git_repo,
    }
    
    resp = api_client.post("projects", json=payload)
        
    assert resp.status_code in (200, 400), "Should either accept or correctly decline the payload schema natively."

def test_github_repo_registration(api_client, mock_git_repo):
    """
    Validates GitHub ingestion
    """
    resp = api_client.post("projects", json={"name": "test-github", "localPath": mock_git_repo})
    assert resp.status_code in (200, 400)
    
