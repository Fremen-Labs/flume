import urllib.request, json
import os
import sys

# Inject flume_secrets settings
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../src')))
from flume_secrets import settings

ES = settings.ES_URL
API_KEY = settings.ES_API_KEY
IDX = "agent-task-records"

def _headers():
    return {"Authorization": f"ApiKey {API_KEY}", "Content-Type": "application/json"}

def es_get(path):
    req = urllib.request.Request(ES + path, headers=_headers())
    return json.loads(urllib.request.urlopen(req).read().decode())

def es_post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(ES + path, data=data, headers=_headers(), method="POST")
    return json.loads(urllib.request.urlopen(req).read().decode())

res = es_get("/" + IDX + "/_search?q=status:blocked&size=50")
hits = res.get("hits",{}).get("hits",[])
print("Found blocked items:", len(hits))

reset = 0
for h in hits:
    src = h["_source"]
    _id = h["_id"]
    owner = src.get("owner","pm")
    role = src.get("assigned_agent_role","pm")
    item_id = src.get("id","unknown")
    item_type = src.get("item_type","?")
    if role == "pm" or owner == "pm":
        body = {"doc": {"status": "planned", "queue_state": "queued", "active_worker": None}}
        es_post("/" + IDX + "/_update/" + _id, body)
        print("  Reset", item_id, "(" + item_type + ") -> planned")
        reset += 1

print("Total reset:", reset)
