"""
E2E Lifecycle Observer — Monitors work queue state transitions end-to-end.

Validates the full Flume planning pipeline:
  Commit Work → Planned → Ready → Running → Done/Blocked

Measures latency at every integration point:
  - Planning: LLM prompt → draft plan ready
  - Commit:   Plan commit → tasks indexed in ES
  - Claim:    Task ready → worker claims (running)
  - Execute:  Task running → done/blocked
  - Pipeline: End-to-end prompt → all tasks terminal

Also probes cross-stack health:
  - Dashboard API responsiveness
  - Gateway node mesh & concurrency
  - Worker heartbeat freshness
  - Elasticsearch cluster health
  - OpenBao seal status

Requires: Flume stack running (./flume start) with at least one Ollama model.
"""
import json
import time
import logging
from datetime import datetime, timezone
from typing import Any

import pytest
import httpx

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# Terminal statuses — task is no longer actively being worked
TERMINAL_STATUSES = {"done", "blocked", "cancelled", "archived"}

# Maximum time to wait for LLM planning (local models can be slow)
PLANNING_TIMEOUT_SEC = 180

# Maximum time to wait for all tasks to reach a terminal state
PIPELINE_TIMEOUT_SEC = 600

# Polling interval
POLL_SEC = 3.0

# Test prompt — intentionally simple to produce 1-3 tasks
TEST_PROMPT = (
    "Update the project README.md to include a 'Quick Start' section "
    "with installation and basic usage instructions."
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _now_ms() -> float:
    return time.time() * 1000


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _elapsed_sec(start_ms: float) -> float:
    return round((time.time() * 1000 - start_ms) / 1000, 3)


class TransitionLog:
    """Records task state transitions with timestamps."""

    def __init__(self):
        self.transitions: list[dict[str, Any]] = []
        self._last_seen: dict[str, str] = {}  # task_id → last status

    def observe(self, task_id: str, status: str) -> bool:
        """Record a transition if the status changed. Returns True on change."""
        prev = self._last_seen.get(task_id)
        if prev == status:
            return False
        self._last_seen[task_id] = status
        self.transitions.append({
            "task_id": task_id,
            "from": prev,
            "to": status,
            "at": _iso_now(),
            "elapsed_ms": _now_ms(),
        })
        logger.info(
            "TRANSITION: %s  %s → %s",
            task_id,
            prev or "(new)",
            status,
        )
        return True

    def all_terminal(self, task_ids: list[str]) -> bool:
        """True when every tracked task is in a terminal state."""
        return all(
            self._last_seen.get(tid) in TERMINAL_STATUSES
            for tid in task_ids
        )

    def summary(self) -> dict:
        """Return a summary of all transitions."""
        return {
            "total_transitions": len(self.transitions),
            "final_states": dict(self._last_seen),
            "transitions": self.transitions,
        }


# ── Integration Point Probes ────────────────────────────────────────────────


class IntegrationProbe:
    """Probes all Flume integration points and records latencies."""

    def __init__(self, api_client: httpx.Client, gateway_client: httpx.Client):
        self.api = api_client
        self.gw = gateway_client
        self.results: dict[str, Any] = {}

    def probe_all(self) -> dict[str, Any]:
        """Run all probes and return combined results."""
        self.results = {
            "timestamp": _iso_now(),
            "dashboard": self._probe_dashboard(),
            "gateway": self._probe_gateway(),
            "workers": self._probe_workers(),
            "elasticsearch": self._probe_elasticsearch(),
            "openbao": self._probe_openbao(),
            "autonomy": self._probe_autonomy(),
        }
        return self.results

    def _probe_dashboard(self) -> dict:
        t0 = _now_ms()
        try:
            resp = self.api.get("/health")
            latency = _now_ms() - t0
            return {
                "status": "ok" if resp.status_code == 200 else "error",
                "http_code": resp.status_code,
                "latency_ms": round(latency, 1),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)[:200], "latency_ms": round(_now_ms() - t0, 1)}

    def _probe_gateway(self) -> dict:
        t0 = _now_ms()
        try:
            resp = self.gw.get("/health")
            latency = _now_ms() - t0
            data = resp.json() if resp.status_code == 200 else {}
            node_resp = self.gw.get("/api/nodes")
            node_data = node_resp.json() if node_resp.status_code == 200 else {}
            return {
                "status": data.get("status", "unknown"),
                "http_code": resp.status_code,
                "latency_ms": round(latency, 1),
                "concurrency": data.get("global", {}),
                "node_count": node_data.get("count", 0),
                "nodes": [
                    {
                        "id": n.get("id"),
                        "model": n.get("model_tag"),
                        "health_status": n.get("health", {}).get("status"),
                        "latency_ms": n.get("health", {}).get("latency_ms"),
                        "loaded_models": n.get("health", {}).get("loaded_models", []),
                    }
                    for n in node_data.get("nodes", [])
                ],
            }
        except Exception as e:
            return {"status": "error", "error": str(e)[:200], "latency_ms": round(_now_ms() - t0, 1)}

    def _probe_workers(self) -> dict:
        t0 = _now_ms()
        try:
            resp = self.api.get("/snapshot")
            latency = _now_ms() - t0
            data = resp.json() if resp.status_code == 200 else {}
            workers = data.get("workers", [])
            return {
                "count": len(workers),
                "latency_ms": round(latency, 1),
                "workers": [
                    {
                        "name": w.get("name"),
                        "role": w.get("role"),
                        "status": w.get("status"),
                        "heartbeat_at": w.get("heartbeat_at"),
                    }
                    for w in workers[:20]  # cap for readability
                ],
            }
        except Exception as e:
            return {"count": 0, "error": str(e)[:200], "latency_ms": round(_now_ms() - t0, 1)}

    def _probe_elasticsearch(self) -> dict:
        t0 = _now_ms()
        try:
            resp = self.api.get("/system-state")
            latency = _now_ms() - t0
            data = resp.json() if resp.status_code == 200 else {}
            return {
                "status": data.get("status", "unknown"),
                "latency_ms": round(latency, 1),
                "es_url": data.get("es_url", ""),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)[:200], "latency_ms": round(_now_ms() - t0, 1)}

    def _probe_openbao(self) -> dict:
        t0 = _now_ms()
        try:
            resp = self.api.get("/system-state")
            latency = _now_ms() - t0
            data = resp.json() if resp.status_code == 200 else {}
            return {
                "status": "ok" if data.get("status") == "online" else "degraded",
                "latency_ms": round(latency, 1),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)[:200], "latency_ms": round(_now_ms() - t0, 1)}

    def _probe_autonomy(self) -> dict:
        t0 = _now_ms()
        try:
            resp = self.api.get("/autonomy/status")
            latency = _now_ms() - t0
            data = resp.json() if resp.status_code == 200 else {}
            return {
                "auto_unblock_alive": data.get("auto_unblock", {}).get("thread_alive", False),
                "sweeps_alive": data.get("sweeps", {}).get("thread_alive", False),
                "latency_ms": round(latency, 1),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)[:200], "latency_ms": round(_now_ms() - t0, 1)}


# ── Test Class ───────────────────────────────────────────────────────────────


class TestLifecycleObserver:
    """
    End-to-end lifecycle observer that monitors the full work queue pipeline.

    Covers: Plan → Commit → Planned → Ready → Running → Done/Blocked
    Measures: Latency at every state transition and integration point
    """

    def test_integration_point_health(self, api_client, gateway_client):
        """
        Probe all Flume integration points BEFORE running the pipeline.
        Ensures the stack is healthy enough to run the lifecycle test.
        """
        probe = IntegrationProbe(api_client, gateway_client)
        results = probe.probe_all()

        logger.info("INTEGRATION PROBE RESULTS:\n%s", json.dumps(results, indent=2))

        # Dashboard must be reachable
        assert results["dashboard"]["status"] == "ok", (
            f"Dashboard unhealthy: {results['dashboard']}"
        )

        # Gateway must be reachable
        assert results["gateway"]["status"] == "ok", (
            f"Gateway unhealthy: {results['gateway']}"
        )

        # At least one node must be registered
        assert results["gateway"]["node_count"] > 0, (
            "No inference nodes registered in the mesh"
        )

        # Workers must be registered
        assert results["workers"]["count"] > 0, (
            "No workers registered — is the worker container running?"
        )

    def test_planning_session_lifecycle(self, api_client, flume_waiter):
        """
        Phase 1: Create a planning session and wait for LLM to produce a plan.
        Validates the LLM connection test runs immediately and the planning
        timer progresses correctly.
        """
        # Get a project to plan against
        snap = api_client.get("/snapshot").json()
        projects = snap.get("projects", [])
        assert projects, "No projects registered — add a project first"
        repo_id = projects[0]["id"]

        t0 = _now_ms()

        # Create the planning session
        resp = api_client.post("/intake/session", json={
            "repo": repo_id,
            "prompt": TEST_PROMPT,
        })
        create_latency = _now_ms() - t0

        assert resp.status_code == 200, (
            f"Intake session creation failed: {resp.status_code} — {resp.text}"
        )

        data = resp.json()
        session_id = data["sessionId"]
        planning_status = data.get("planningStatus", {})

        logger.info(
            "SESSION CREATED in %.1fms: id=%s, stage=%s, provider=%s, model=%s",
            create_latency,
            session_id,
            planning_status.get("stage"),
            planning_status.get("provider"),
            planning_status.get("model"),
        )

        # Connection test should have run immediately (synchronous fast-fail)
        assert planning_status.get("connectionTestOk") is True, (
            f"Connection test failed: {planning_status.get('connectionTestResult')}"
        )

        conn_duration = planning_status.get("connectionTestDurationMs")
        logger.info(
            "CONNECTION TEST: ok=%s, duration=%.1fms, result=%s",
            planning_status.get("connectionTestOk"),
            conn_duration or 0,
            planning_status.get("connectionTestResult"),
        )

        # Wait for plan generation
        session_data = flume_waiter.wait_for_session_plan(
            session_id, timeout_sec=PLANNING_TIMEOUT_SEC
        )
        planning_latency = _now_ms() - t0

        plan = session_data.get("plan", {})
        epics = plan.get("epics", [])
        assert epics, "LLM returned empty plan"

        # Count total tasks
        task_count = sum(
            len(task_list)
            for epic in epics
            for feat in epic.get("features", [])
            for story in feat.get("stories", [])
            for task_list in [story.get("tasks", [])]
        )

        final_status = session_data.get("planningStatus", {})
        logger.info(
            "PLAN READY in %.1fs: %d epics, %d tasks, elapsed=%s",
            planning_latency / 1000,
            len(epics),
            task_count,
            final_status.get("requestElapsedSeconds"),
        )

        # Store session_id for the commit test
        self.__class__._session_id = session_id
        self.__class__._plan = plan
        self.__class__._repo_id = repo_id
        self.__class__._planning_latency_ms = planning_latency
        self.__class__._conn_test_duration_ms = conn_duration

    def test_commit_and_queue_lifecycle(self, api_client, gateway_client):
        """
        Phase 2: Commit the plan and observe task state transitions through
        the full work queue pipeline.

        Measures:
          - Commit latency (commit → tasks indexed)
          - Claim latency (ready → running)
          - Execution latency (running → done/blocked)
          - Total pipeline latency
        """
        session_id = getattr(self.__class__, "_session_id", None)
        plan = getattr(self.__class__, "_plan", None)
        repo_id = getattr(self.__class__, "_repo_id", None)
        if not session_id or not plan:
            pytest.skip("Planning session not available — run test_planning_session_lifecycle first")

        t0 = _now_ms()

        # ── COMMIT THE PLAN ──────────────────────────────────────────────
        resp = api_client.post(f"/intake/session/{session_id}/commit", json={
            "plan": plan,
            "repo": repo_id,
        })
        commit_latency = _now_ms() - t0

        assert resp.status_code == 200, (
            f"Plan commit failed: {resp.status_code} — {resp.text}"
        )

        commit_data = resp.json()
        assert commit_data.get("ok") is True, f"Commit returned ok=false: {commit_data}"

        task_count = commit_data.get("count", 0)
        created_count = commit_data.get("created", 0)
        task_ids = commit_data.get("taskIds", [])

        logger.info(
            "COMMIT in %.1fms: %d tasks created, %d total items, task_ids=%s",
            commit_latency, created_count, task_count, task_ids,
        )

        assert task_count > 0, "No tasks created from the plan"

        # ── OBSERVE QUEUE TRANSITIONS ────────────────────────────────────
        log = TransitionLog()
        timestamps = {
            "commit_at": _iso_now(),
            "first_ready_at": None,
            "first_running_at": None,
            "first_done_at": None,
            "all_terminal_at": None,
        }

        # Discover all task IDs from the snapshot (includes non-leaf items)
        time.sleep(1)  # brief ES indexing delay
        snap = api_client.get("/snapshot").json()
        all_task_ids = [
            t["id"] for t in snap.get("tasks", [])
            if t.get("repo") == repo_id and t.get("item_type") == "task"
        ]

        if not all_task_ids:
            all_task_ids = task_ids  # fallback

        logger.info("OBSERVING %d tasks: %s", len(all_task_ids), all_task_ids)

        # Poll and record transitions
        deadline = time.time() + PIPELINE_TIMEOUT_SEC
        while time.time() < deadline:
            snap = api_client.get("/snapshot").json()
            tasks = snap.get("tasks", [])

            for t in tasks:
                tid = t.get("id")
                if tid not in all_task_ids:
                    continue
                status = t.get("status", "unknown")
                changed = log.observe(tid, status)

                if changed:
                    if status == "ready" and not timestamps["first_ready_at"]:
                        timestamps["first_ready_at"] = _iso_now()
                    elif status == "running" and not timestamps["first_running_at"]:
                        timestamps["first_running_at"] = _iso_now()
                    elif status in TERMINAL_STATUSES and not timestamps["first_done_at"]:
                        timestamps["first_done_at"] = _iso_now()

            if log.all_terminal(all_task_ids):
                timestamps["all_terminal_at"] = _iso_now()
                logger.info("ALL TASKS TERMINAL — pipeline complete")
                break

            time.sleep(POLL_SEC)

        # ── FINAL INTEGRATION PROBE ──────────────────────────────────────
        probe = IntegrationProbe(api_client, gateway_client)
        final_health = probe.probe_all()

        # ── COMPILE MEASUREMENTS ─────────────────────────────────────────
        pipeline_elapsed_ms = _now_ms() - t0
        measurements = {
            "planning_latency_ms": getattr(self.__class__, "_planning_latency_ms", None),
            "connection_test_ms": getattr(self.__class__, "_conn_test_duration_ms", None),
            "commit_latency_ms": round(commit_latency, 1),
            "pipeline_elapsed_ms": round(pipeline_elapsed_ms, 1),
            "pipeline_elapsed_sec": round(pipeline_elapsed_ms / 1000, 1),
            "task_count": len(all_task_ids),
            "created_count": created_count,
            "timestamps": timestamps,
            "transitions": log.summary(),
            "final_health": final_health,
        }

        logger.info(
            "LIFECYCLE MEASUREMENTS:\n%s",
            json.dumps(measurements, indent=2, default=str),
        )

        # Store for report generation
        self.__class__._measurements = measurements

        # ── ASSERTIONS ───────────────────────────────────────────────────

        # At least one task should have reached running
        final_states = log.summary()["final_states"]
        reached_running = any(
            t.get("to") == "running"
            for t in log.summary()["transitions"]
        )
        logger.info("Final task states: %s", final_states)

        # We don't assert all_terminal because local models may take >10min
        # for complex tasks. We DO assert the pipeline started flowing.
        assert reached_running or any(
            s in TERMINAL_STATUSES for s in final_states.values()
        ), (
            f"No task reached 'running' or terminal state. "
            f"Final states: {final_states}"
        )

    def test_generate_measurements_report(self, api_client, gateway_client):
        """
        Phase 3: Generate a JSON report with all measurements collected
        during the lifecycle observation.
        """
        measurements = getattr(self.__class__, "_measurements", None)
        if not measurements:
            pytest.skip("No measurements collected — run test_commit_and_queue_lifecycle first")

        # Final snapshot for worker utilization
        snap = api_client.get("/snapshot").json()
        worker_summary = []
        for w in snap.get("workers", []):
            worker_summary.append({
                "name": w.get("name"),
                "role": w.get("role"),
                "status": w.get("status"),
                "input_tokens": w.get("input_tokens", 0),
                "output_tokens": w.get("output_tokens", 0),
            })

        report = {
            "report_generated_at": _iso_now(),
            "pipeline": measurements,
            "worker_utilization": worker_summary,
            "task_count_by_status": {},
        }

        # Count tasks by status
        for t in snap.get("tasks", []):
            status = t.get("status", "unknown")
            report["task_count_by_status"][status] = (
                report["task_count_by_status"].get(status, 0) + 1
            )

        logger.info(
            "FINAL REPORT:\n%s",
            json.dumps(report, indent=2, default=str),
        )

        # Basic validation
        assert report["pipeline"]["task_count"] > 0
        assert report["pipeline"]["commit_latency_ms"] > 0
