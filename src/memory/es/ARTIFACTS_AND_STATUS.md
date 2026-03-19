# Artifacts and Status

## Artifact attachment
```bash
memory/es/scripts/attach_artifact.sh <task_id> <path> <type> <label>
```

Artifacts are currently stored in task records as:
```text
type|label|path|timestamp
```

## Status dashboard
```bash
memory/es/scripts/status_dashboard.sh
```

## Artifact-aware review bootstrap
```bash
memory/es/scripts/review_with_artifacts.sh <task_id> <project> <repo>
```

## Purpose

These helpers make review more evidence-driven and make system state easier to inspect quickly.
