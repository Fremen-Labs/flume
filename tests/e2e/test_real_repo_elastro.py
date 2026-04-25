import pytest

@pytest.mark.e2e
def test_real_repo_website_update(api_client, flume_waiter, real_ado_repo):
    """
    Tests Flume against a realistic, remote Azure DevOps repository mimicking
    a standard frontend engineering workflow.
    """
    # 1. Onboard the Real Repository
    payload = {
        "name": "elastro-website-e2e",
        "repoUrl": real_ado_repo, # Using remote mapping explicitly
        # In a real run, Flume pulls via generic proxy or uses local path. 
        # We will attempt registering to see if the engine accepts ADO schemas
    }
    
    resp = api_client.post("projects", json=payload)
    if resp.status_code == 404:
        # Fallback to local ingestion simulation for elastro
        payload = {"name": "elastro-website-e2e", "localPath": "/tmp/elastro-website"}
        resp = api_client.post("projects", json=payload)
        
    assert resp.status_code in (200, 400), "Should either accept or decline schema elegantly"
    project_id = resp.json().get("projectId")
    if not project_id:
        pytest.fail("Failed to retrieve projectId from project creation response")
        
    try:
        # 2. Simulate User Planning
        intake_resp = api_client.post("intake/session", json={
            "repo": project_id,
            "prompt": "Update the Vite configuration to include a new path alias '@components'. Add a placeholder Vue component called BannerTracker."
        })
        
        # 3. Commit the planned session to dispatch to Swarm Workers
        data = intake_resp.json()
        session_id = data.get("sessionId")
        assert session_id, "Dashboard should return a Session ID for the planning draft"
        
        # Poll until LLM builds the plan asynchronously
        print(f"Waiting for LLM to draft plan for session {session_id}...")
        flume_waiter.wait_for_session_plan(session_id, timeout_sec=350)
        
        commit_resp = api_client.post(f"intake/session/{session_id}/commit", json={})
        assert commit_resp.status_code == 200
        task_ids = commit_resp.json().get("taskIds", [])
        assert len(task_ids) > 0, "Commit should yield actionable worker tasks"
        
        task_id = task_ids[0]
        
        # 4. Reasoning Inspection: Wait until it hits In Progress and assert no FATAL loops occur internally
        print(f"Monitoring execution reasoning for {task_id}...")
        task = flume_waiter.wait_for_task_status_with_reasoning(task_id, ["done", "in_progress", "blocked"], timeout_sec=240)
        
        # 5. Final Verification: Wait for Done State
        if task.get("status") == "in_progress":
            task = flume_waiter.wait_for_task_status_with_reasoning(task_id, ["done", "blocked"], timeout_sec=400)
            
        assert task.get("status") in ("done", "blocked"), "Task failed to reach completed or blocked state"

    finally:
        # Teardown: Clean up the project from Elasticsearch to prevent test pollution
        api_client.post(f"projects/{project_id}/delete")
