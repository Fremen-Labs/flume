# Task Automation Layer

## Purpose

Provide a minimal task-oriented workflow on top of the hybrid memory system.

## Core Commands

### Create a task
```bash
memory/es/scripts/task_create.sh <id> <title> <objective> <repo> [owner] [priority] [risk]
```

### Update a task
```bash
memory/es/scripts/task_update.sh <task_doc_id> <status> <owner> [needs_human] [priority]
```

### Search tasks
```bash
memory/es/scripts/task_search.sh <query>
```

### Bootstrap task context
```bash
memory/es/scripts/task_bootstrap.sh <task_query> <project> <repo>
```

## Suggested Usage Pattern

### Planner
1. create task
2. search/bootstrap prior context
3. write handoff to implementer
4. update task status to `planned` or `ready`

### Implementer
1. bootstrap task context
2. update task status to `running`
3. write decisions/failures during work
4. handoff to reviewer
5. update task status to `review`

### Reviewer
1. bootstrap task context
2. validate work
3. either write failure + handoff back, or mark `done`

### Memory Updater
1. review decisions/failures
2. promote durable entries to canonical memory
3. leave routine operational detail in Elasticsearch
