# Role Wrappers Usage

## Planner
```bash
memory/es/scripts/planner_run.sh task-001 "Title" "Objective" openclaw workspace high medium implementer
```

## Implementer
```bash
memory/es/scripts/implementer_run.sh task-001 openclaw workspace "task-001" "Decision title" "Decision statement"
```

## Reviewer
```bash
memory/es/scripts/reviewer_run.sh task-001 openclaw workspace approved "Looks good" memory-updater true
```

## Memory Updater
```bash
memory/es/scripts/memory_updater_run.sh "hybrid memory" ${LOOM_WORKSPACE}/MEMORY.md openclaw workspace
```

## Intended Flow

1. Planner wrapper creates task and handoff
2. Implementer wrapper bootstraps context and marks running
3. Reviewer wrapper applies verdict and routes task
4. Memory updater wrapper promotes durable lessons
