import urllib.request, json, uuid
from datetime import datetime, timezone

ES_URL = "http://127.0.0.1:9200"
ES_API_KEY = "flume-enterprise-dev-bypass-key"

def es_post(path, doc):
    req = urllib.request.Request(
        f"{ES_URL}/{path}",
        data=json.dumps(doc).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"ApiKey {ES_API_KEY}"},
        method="POST"
    )
    urllib.request.urlopen(req, timeout=5)

tasks = [
    {"level": 1, "title": "Complexity 1: Write a python hello world print script", "objective": "Create a dummy.py file."},
    {"level": 5, "title": "Complexity 5: Implement a caching memoizer via Redis", "objective": "Add redis integration to backend."},
    {"level": 10, "title": "Complexity 10: Migrate openbao async architecture", "objective": "Rewrite the entire synchronous REST stack to async httpx natively."}
]

for t in tasks:
    task_id = f"task-scale-{t['level']}-{uuid.uuid4().hex[:6]}"
    es_post(f"agent-task-records/_doc/{task_id}", {
        "id": task_id,
        "title": t["title"],
        "objective": t["objective"],
        "repo": "flume",
        "owner": "pm",
        "assigned_agent_role": "pm",
        "status": "planned",
        "queue_state": "queued",
        "item_type": "task",
        "needs_human": False,
        "updated_at": datetime.now(timezone.utc).isoformat()
    })
print("Injected 3 scaling tasks for E2E orchestration.")
