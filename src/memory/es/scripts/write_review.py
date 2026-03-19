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
INDEX = os.environ.get("ES_INDEX_REVIEWS", "agent-review-records")

if not ES_API_KEY:
    raise SystemExit("ES_API_KEY is required")

payload = json.load(sys.stdin)
payload.setdefault("created_at", datetime.now(timezone.utc).isoformat())
review_id = payload.get("review_id") or payload.get("task_id")
if not review_id:
    raise SystemExit("review payload must include review_id or task_id")

ctx = None
if not ES_VERIFY_TLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

req = urllib.request.Request(
    f"{ES_URL}/{INDEX}/_doc/{review_id}",
    data=json.dumps(payload).encode(),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"ApiKey {ES_API_KEY}",
    },
    method="PUT",
)
with urllib.request.urlopen(req, context=ctx) as resp:
    print(resp.read().decode())
