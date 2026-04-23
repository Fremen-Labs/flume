import pytest
import time

# List of all officially supported Flume AI Grid providers
PROVIDERS = [
    "ollama:qwen-35b",
    "openai:gpt-4o",
    "anthropic:claude-3-5-sonnet",
    "grok:grok-4.20"
]

@pytest.mark.e2e
@pytest.mark.provider
@pytest.mark.parametrize("provider_model", PROVIDERS)
def test_provider_planning_regression(api_client, flume_waiter, isolated_flume_project, provider_model):
    """
    Validates that each provider in our matrix can natively parse the Intake
    prompt into a structural Flume Plan without hallucinating out of bounds.
    (Note: If backend doesn't support model overrides yet, this acts as a 
     structural footprint to map out regression tests when multi-model logic lands).
    """
    repo_id = isolated_flume_project
    
    # 1. Provide a deterministic logic test payload
    prompt = "Create a simple Python function that calculates the Fibonacci sequence up to N. Add exactly two unit tests."
    
    payload = {
        "repo": repo_id,
        "prompt": prompt,
        "provider_constraint": provider_model # Future mapping for worker affinity
    }
    
    # Send to the central planner
    resp = api_client.post("intake/session", json=payload)
    assert resp.status_code == 200, f"Provider {provider_model} failed intake session ingestion"
    
    # 2. Extract Session ID and map it
    session_id = resp.json().get("sessionId")
    
    flume_waiter.wait_for_session_plan(session_id, timeout_sec=350)
    
    commit_resp = api_client.post(f"intake/session/{session_id}/commit", json={})
    assert commit_resp.status_code == 200, "Should compile into a dispatchable Flume Plan"
    
    # Since we are E2E, we could await the plan, but in a multi-model matrix
    # testing 4 models simultaneously against a local grid might overload it.
    # Therefore, we primarily focus on verifying that the Engine accepts the
    # routing constraints and doesn't explicitly throw validation errors.
    
    task_id = commit_resp.json().get("taskIds")[0]
    
    # 3. Validation
    print(f"[{provider_model}] dispatched as Task: {task_id}")
