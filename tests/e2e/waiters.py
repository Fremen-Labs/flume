import time
import httpx
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)

class FlumeWaiter:
    def __init__(self, api_client: httpx.Client, poll_interval: float = 2.0):
        self.client = api_client
        self.poll_interval = poll_interval

    def _get_snapshot(self) -> Dict[str, Any]:
        resp = self.client.get("snapshot")
        resp.raise_for_status()
        return resp.json()

    def wait_for_project_clone(self, project_id: str, timeout_sec: int = 30) -> bool:
        """Polls until Elasticsearch recognizes the project boundary integration"""
        start = time.time()
        while time.time() - start < timeout_sec:
            snap = self._get_snapshot()
            if any(p.get("id") == project_id for p in snap.get("projects", [])):
                return True
            time.sleep(self.poll_interval)
        raise TimeoutError(f"Project '{project_id}' never materialized in state.")

    def wait_for_task_status_with_reasoning(self, task_id: str, target_statuses: list[str], timeout_sec: int = 90) -> Dict[str, Any]:
        """
        Polls until the task reaches one of the target statuses.
        Actively inspects the execution reasoning for failures along the way.
        """
        start = time.time()
        last_thoughts_length = 0
        
        while time.time() - start < timeout_sec:
            snap = self._get_snapshot()
            
            # Find task
            task = next((t for t in snap.get("tasks", []) if t.get("id") == task_id), None)
            
            if not task:
                time.sleep(self.poll_interval)
                continue
                
            current_status = task.get("status")
            
            # 1. Inspect reasoning for errors if running
            if current_status == "in_progress":
                try:
                    # In true implementation hit `GET /api/tasks/{task_id}/thoughts` or similar
                    thought_resp = self.client.get(f"tasks/{task_id}/thoughts")
                    if thought_resp.status_code == 200:
                        thoughts_data = thought_resp.json().get("execution_thoughts", "")
                        
                        if len(thoughts_data) > last_thoughts_length:
                            logger.info(f"Agent Reasoning Update:\n{thoughts_data[last_thoughts_length:]}")
                            last_thoughts_length = len(thoughts_data)
                            
                        # Basic safety guard to fail fast on detected cyclic hallucination or worker panic string
                        if "KillSwitchAbortError" in thoughts_data:
                            raise Exception("Worker triggered Kill Switch Abort internally.")
                        if "FATAL" in thoughts_data.upper():
                            raise Exception(f"Agent reasoning loop encountered FATAL state:\n{thoughts_data}")
                except httpx.HTTPError:
                    pass # Ignore read errors while fetching thoughts
                    
            # 2. Check terminating statuses
            if current_status in target_statuses:
                return task
            if current_status in ["failed", "blocked"] and current_status not in target_statuses and "failed" not in target_statuses:
                raise Exception(f"Task {task_id} unexpectedly entered '{current_status}' state.")
                
            time.sleep(self.poll_interval)
            
        raise TimeoutError(f"Task '{task_id}' failed to reach {target_statuses} within {timeout_sec}s")

    def wait_for_session_plan(self, session_id: str, timeout_sec: int = 90) -> Dict[str, Any]:
        """Polls the intake session until the LLM successfully generates a draft plan"""
        start = time.time()
        while time.time() - start < timeout_sec:
            resp = self.client.get(f"intake/session/{session_id}")
            if resp.status_code == 200:
                data = resp.json()
                # A successful drafting usually sets plan with epics or status to something other than 'processing'
                plan = data.get("plan", {})
                if plan and plan.get("epics"):
                    return data
                
                # Fail fast if backend reports a fatal error
                planning_status = data.get("planningStatus", {})
                if planning_status and planning_status.get("stage") == "failed":
                    raise Exception(f"Intake session failed: {planning_status.get('failureText')}")
                    
            time.sleep(self.poll_interval)
        raise TimeoutError(f"Intake session '{session_id}' failed to generate a draft plan within {timeout_sec}s")

