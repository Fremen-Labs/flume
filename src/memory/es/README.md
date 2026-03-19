# Elasticsearch Memory Helpers

## Setup

1. Copy `.env.local.example` to `.env.local`
2. Fill in `ES_URL` and `ES_API_KEY`
3. Use the helper scripts below

## Common Commands

### Create templates and indices
```bash
memory/es/scripts/create_indices.sh
```

### Write a decision
```bash
memory/es/scripts/write_decision.sh system openclaw workspace "Title" "Statement"
```

### Write a task
```bash
memory/es/scripts/write_task.sh task-001 "Title" "Objective" workspace planner running high
```

### Write a failure
```bash
memory/es/scripts/write_failure.sh task-001 openclaw workspace ErrorClass "Summary" "Root cause" "Fix applied"
```

### Retrieve context
```bash
memory/es/scripts/retrieve_context.sh "hybrid memory" openclaw workspace
```

## Notes

- `.env.local` is intentionally untracked
- canonical durable memory still belongs in files/Git
- Elasticsearch is the fast operational retrieval layer
