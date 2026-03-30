# Flume API: Headless Onboarding & Dispatch

The Flume dashboard is entirely driven by a robust, headless FastAPI backend. Because the Go orchestrator isolates this backend securely on `localhost:8765` within the Docker execution boundary, you can programmatically integrate Flume into external CI/CD hooks, custom ticketing pipelines, or entirely separate UIs seamlessly.

This guide structurally maps the complete "Onboard to Dispatch" flow via standard `curl`.

---

## 1. Project Registration (AST Ingestion)

Before Flume can operate on code natively, it must build a cryptographic Elastro RAG Graph index of the codebase. You can trigger this ingestion remotely.

**Endpoint:** `POST /api/projects`
**Action:** Registers the absolute environment variable mapped `repoUrl` into `projects.json` and immediately fires the `_deterministic_ast_ingest` background task.

```bash
curl -X POST http://localhost:8765/api/projects \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Production Frontend",
    "repoUrl": "/workspace/fremenlabs/frontend"
  }'
```

**Response**
```json
{
  "success": true,
  "projectId": "proj-a1b2c3d4",
  "message": "Project dynamically constructed seamlessly natively."
}
```

---

## 2. LLM Planner Dispatch (Intake Session)

You don't need a UI to break down complex issues. You can send an unstructured feature request strictly through the API and force Flume's PM Agent to map out the Epics and Tasks automatically.

**Endpoint:** `POST /api/intake/session`
**Action:** Spawns an intake session, querying your configured LLM (e.g. Exo, OpenAI) securely to build a JSON tree matrix securely.

```bash
curl -X POST http://localhost:8765/api/intake/session \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "proj-a1b2c3d4",
    "prompt": "Implement a secure OAuth2 login page using React and TailwindCSS."
  }'
```

**Response**
```json
{
  "id": "plan-ff9944aa22bb",
  "planningStatus": {
    "stage": "testing_connection"
  }
}
```

### Polling for Completion

Because large local models (or API proxies) take multi-second executions to assemble large plans natively, you must poll the session endpoint.

**Endpoint:** `GET /api/intake/session/{session_id}`

```bash
curl -s http://localhost:8765/api/intake/session/plan-ff9944aa22bb | jq '.draftPlan'
```

Once `.planningStatus.stage == "ready"`, the `.draftPlan` object will contain a fully expanded array of Epics, Features, and granular Tasks with generated descriptions natively.

---

## 3. Committing the Plan

Once the draft is generated natively, commit it to securely lock the tasks into the `agent-task-records` Elastic store.

**Endpoint:** `POST /api/intake/session/{session_id}/commit`

```bash
curl -X POST http://localhost:8765/api/intake/session/plan-ff9944aa22bb/commit \
  -H "Content-Type: application/json" \
  -d '{}'
```

*This will return a matrix array of newly instantiated `task_ids` ready for execution!*

---

## 4. Triggering the Autonomous Matrix

By default, committed tasks sit safely in an `approved` boundary wait state. To natively wake the Swarm intelligence and begin code modification securely, transition the task to `in_progress`.

**Endpoint:** `POST /api/tasks/{task_id}/transition`

```bash
curl -X POST http://localhost:8765/api/tasks/task-9c8e7f6d/transition \
  -H "Content-Type: application/json" \
  -d '{
    "status": "in_progress"
  }'
```

The Flume API organically locks the task mutext natively, assigns an `Implementer` agent, spawns a new `git worktree`, and begins modifying the repository files mapped inside the `/workspace` safely without any further manual intervention!
