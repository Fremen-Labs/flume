# Agent Memory Integration

## Recommended Flow

### Before work starts
Run:
```bash
memory/es/scripts/bootstrap_memory.sh "<query>" <project> <repo>
```

This gives:
- canonical memory hints from files
- operational memory/task recall from Elasticsearch

### During work
Write operational records:
- decisions → `write_decision.sh`
- tasks → `write_task.sh`
- failures → `write_failure.sh`
- handoffs → `write_handoff.sh`

### After durable learning
Promote stable knowledge into canonical files:
- direct promotion: `promote_decision.sh`
- search-based promotion: `promote_from_search.py`

## Suggested Role Behavior

### Planner
- query bootstrap memory first
- write/update task records
- write handoff records when delegating

### Implementer
- query context before coding
- write decision records when making architecture-impacting changes
- write failure records when blocked by non-trivial recurring issues

### Reviewer
- query recent tasks + decisions
- write failure records for recurring defects
- promote durable review lessons when needed

### Memory Updater
- review recent operational memory
- promote stable lessons into canonical file memory
- prune or deactivate obsolete entries later
