# Queue Automation

## New helpers

- `generate_work_tree.py`
- `compute_ready.py`
- `worker_loop.sh`

## What they do

### generate_work_tree
Creates a nested hierarchy of epic/feature/story/task/bug items from a JSON spec.

### compute_ready
Marks leaf items `ready` only when dependencies are satisfied.

### worker_loop
Runs a minimal queue cycle:
1. compute ready items
2. claim next item for a given role

## Why this matters

This is the bridge from static records to agent-consumable work queues.
