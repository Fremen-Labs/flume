# Review Automation

## Purpose

Provide a reviewer/critic loop for task-driven execution.

## Core Commands

### Write a raw review record
```bash
memory/es/scripts/write_review.sh <review_id> <task_id> <verdict> <summary> <recommended_next_role> [promotion_candidate] [confidence]
```

### Apply a review verdict to workflow state
```bash
memory/es/scripts/review_task.sh <task_id> <approved|changes_requested|blocked> <summary> <recommended_next_role> [promotion_candidate]
```

### Bootstrap review context
```bash
memory/es/scripts/review_bootstrap.sh <task_id_or_query> <project> <repo>
```

## Verdict Behavior

### approved
- writes review record
- updates task to `done`

### changes_requested
- writes review record
- writes handoff back to implementer
- updates task to `running`

### blocked
- writes review record
- updates task to `blocked`
- marks `needs_human=true`

## Recommended Use

1. reviewer bootstraps task + memory context
2. reviewer writes verdict
3. workflow routes automatically
4. durable lessons can be promoted afterward
