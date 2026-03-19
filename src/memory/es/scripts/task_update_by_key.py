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
if len(sys.argv) < 3:
    raise SystemExit("usage: task_update_by_key.py <task_id> <json_fragment_file>")

task_id = sys.argv[1]
fragment = json.loads(open(sys.argv[2]).read())
fragment["updated_at"] = datetime.now(timezone.utc).isoformat()
fragment.setdefault("last_update", fragment["updated_at"])

ctx = None
if not ES_VERIFY_TLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

req = urllib.request.Request(
    f"{ES_URL}/{INDEX}/_update/{task_id}",
    data=json.dumps({"doc": fragment, "doc_as_upsert": True}).encode(),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"ApiKey {ES_API_KEY}",
    },
    method="POST",
)
with urllib.request.urlopen(req, context=ctx) as resp:
    print(resp.read().decode())
