import os, sys, json
sys.path.insert(0, os.path.abspath('src/worker-manager'))
from worker_handlers import fetch_task_doc, ES_URL

print(f"ES_URL={ES_URL}")
es_id, task = fetch_task_doc("task-scale-1-eb9122")
print(f"es_id: {es_id}, task: {type(task)}")
if task:
    print(json.dumps(task))
