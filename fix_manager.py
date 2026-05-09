import sys

with open('src/worker-manager/manager.py', 'r') as f:
    lines = f.readlines()

new_lines = []
for i, line in enumerate(lines):
    idx = i + 1
    if 295 <= idx <= 847:
        continue
    if 857 <= idx <= 1324:
        continue
    
    if idx == 50:
        new_lines.append("""from orchestration import (
    try_atomic_claim,
    requeue_stuck_implementer_tasks,
    requeue_stuck_review_tasks,
    promote_planned_tasks,
    execute_block_sweep,
    execute_resume_sweep,
    count_available_by_status,
    SWEEP_LAST_RUN as _SWEEP_LAST_RUN,
    SWEEP_INTERVALS as _SWEEP_INTERVALS,
)
""")
    
    if "_count_available_by_status()" in line:
        line = line.replace("_count_available_by_status()", "count_available_by_status()")
    if "_execute_resume_sweep()" in line:
        line = line.replace("_execute_resume_sweep()", "execute_resume_sweep()")
    if "_execute_block_sweep(" in line:
        line = line.replace("_execute_block_sweep(", "execute_block_sweep(")
        
    new_lines.append(line)

with open('src/worker-manager/manager.py', 'w') as f:
    f.writelines(new_lines)
