import os
import json
import time
import subprocess
import tempfile
import asyncio
from pathlib import Path
from worker_handlers import *
from agent_runner import (
    run_pm_dispatcher,
    run_implementer,
    run_tester,
    run_reviewer,
    _get_active_llm_model,
    _run_with_client
)

def handle_pm_dispatcher_worker(task):
    if not task:
        return True

    task_id = task.get('id')
    es_id, _ = fetch_task_doc(task_id) if task_id else (None, None)

    # Initialize execution_thoughts for this run so the drawer can display live reasoning
    if es_id:
        try:
            es_request(f'/{TASK_INDEX}/_update/{es_id}', {'doc': {'execution_thoughts': []}}, method='POST')
        except Exception:
            pass
        append_execution_thought(es_id, f"*[PM Dispatcher]* Analyzing task: **{task.get('title', task_id)}**")

    # Intelligent Task Scope & PM Hallucination Boundaries
    active_model = str(task.get('preferred_model') or _get_active_llm_model()).lower()
    if 'gpt-4' in active_model or 'claude-3-opus' in active_model:
        # High capacity models: chunk into component-level epics
        task['chunking_strategy'] = 'epic_component_level'
    else:
        # Smaller models / local inferences: 20-line recursive functional scopes
        task['chunking_strategy'] = '20_line_functional_scope'

    if es_id:
        append_execution_thought(es_id, f"*[PM Dispatcher]* Sending to LLM for decomposition analysis (model: `{active_model}`)…")

    try:
        result = asyncio.run(_run_with_client(run_pm_dispatcher, task))
    except Exception as e:
        log(f"pm-dispatcher: Execution Trap mapping decomposition on {task_id} natively: {e}")
        if es_id:
            append_execution_thought(es_id, f"*[PM Dispatcher]* ❌ Decomposition failed: {str(e)[:200]}")
            update_task_doc(es_id, {
                'status': 'blocked',
                'active_worker': None,
                'queue_state': 'queued',
            })
        return True

    if result.action == 'decompose' and getattr(result, 'subtasks', []):
        count = 0
        child_titles = []
        for st in result.subtasks:
            child_id = f"{st.get('item_type', 'task')}-{uuid.uuid4().hex[:8]}"
            doc = {
                'id': child_id,
                'parent_id': task_id,
                'title': st.get('title', 'Generated Subtask'),
                'objective': st.get('objective', ''),
                'item_type': st.get('item_type', 'task'),
                'repo': task.get('repo'),
                'status': 'planned',
                'owner': 'pm',
                'assigned_agent_role': 'pm',
                'depends_on': [],
                'acceptance_criteria': [],
                'artifacts': [],
                'needs_human': False,
                'created_at': now_iso(),
                'updated_at': now_iso(),
                'last_update': now_iso(),
            }
            write_doc(TASK_INDEX, doc)
            child_titles.append(st.get('title', child_id))
            count += 1

        if es_id:
            subtask_list = "\n".join(f"  - {t}" for t in child_titles)
            append_execution_thought(es_id, f"*[PM Dispatcher]* ✅ Decomposed into **{count}** children:\n{subtask_list}")
            update_task_doc(es_id, {
                'status': 'running',
                'active_worker': None,
                'queue_state': 'queued',
            })
            log(f"pm-dispatcher: decomposed {task_id} into {count} children; suspended parent.")
        return True

    promoted = compute_ready_for_repo((task or {}).get('repo'))
    log(f"pm-dispatcher: {result.summary[:200]}; promoted={promoted}")
    
    if es_id:
        append_execution_thought(es_id, f"*[PM Dispatcher]* Task is compute-ready. Summary: {result.summary[:300]}")
        update_task_doc(es_id, {
            'active_worker': None,
            'queue_state': 'queued',
        })
    return True
