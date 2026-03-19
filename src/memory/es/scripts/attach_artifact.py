#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
import ssl
from datetime import datetime, timezone

ES_URL = os.environ.get("ES_URL", "https://localhost:9200").rstrip("/")
ES_API_KEY = os.environ.get("ES_API_KEY")
ES_VERIFY_TLS = os.environ.get("ES_VERIFY_TLS", "false").lower() == "true"
INDEX = os.environ.get("ES_INDEX_TASKS", "agent-task-records")

if not ES_API_KEY:
    raise SystemExit("ES_API_KEY is required")
if len(sys.argv) < 5:
    raise SystemExit("usage: attach_artifact.py <task_id> <path> <type> <label>")

task_id, path, atype, label = sys.argv[1:5]
now = datetime.now(timezone.utc).isoformat()
artifact_entry = f"{atype}|{label}|{path}|{now}"

ctx = None
if not ES_VERIFY_TLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
headers = {"Content-Type": "application/json", "Authorization": f"ApiKey {ES_API_KEY}"}

get_req = urllib.request.Request(f"{ES_URL}/{INDEX}/_doc/{task_id}", headers=headers, method="GET")
with urllib.request.urlopen(get_req, context=ctx) as resp:
    data = json.loads(resp.read().decode())
if not data.get("found"):
    raise SystemExit(f"Task not found: {task_id}")
source = data.get("_source", {})
artifacts = source.get("artifacts") or []
artifacts.append(artifact_entry)
source["artifacts"] = artifacts
source["updated_at"] = now
source["last_update"] = now

put_req = urllib.request.Request(
    f"{ES_URL}/{INDEX}/_doc/{task_id}",
    data=json.dumps(source).encode(),
    headers=headers,
    method="PUT",
)
with urllib.request.urlopen(put_req, context=ctx) as resp:
    print(resp.read().decode())
