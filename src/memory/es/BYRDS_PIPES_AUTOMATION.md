# Byrds / Pipes Automation

## Purpose

Normalize orchestration and execution events into the hybrid memory/task system.

## Ingesters

### Byrds
```bash
memory/es/scripts/byrds_event_ingest.py <event.json>
```

Handled event types:
- `task.created`
- `task.updated`
- `task.status_changed`
- `task.handoff`

### Pipes
```bash
memory/es/scripts/pipes_event_ingest.py <event.json>
```

Handled event types:
- `job.created`
- `job.started`
- `job.heartbeat`
- `job.retrying`
- `job.succeeded`
- `job.failed`
- `job.timed_out`
- `job.cancelled`

## Suggested Event Shape

```json
{
  "event_type": "task.created",
  "task_id": "task-001",
  "project": "openclaw",
  "repo": "workspace",
  "role": "planner",
  "title": "Example",
  "objective": "Do the thing",
  "summary": "Optional human-readable summary"
}
```

## Result

These ingesters translate orchestration/runtime signals into:
- task records
- handoff records
- provenance records
- failure records

so agents can continue from system state instead of chat memory.
