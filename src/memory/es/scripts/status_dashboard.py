#!/usr/bin/env python3
import json
import os
import urllib.request
import ssl

ES_URL = os.environ.get("ES_URL", "https://localhost:9200").rstrip("/")
ES_API_KEY = os.environ.get("ES_API_KEY")
ES_VERIFY_TLS = os.environ.get("ES_VERIFY_TLS", "false").lower() == "true"
TASKS = os.environ.get("ES_INDEX_TASKS", "agent-task-records")
FAILS = os.environ.get("ES_INDEX_FAILURES", "agent-failure-records")
REVIEWS = os.environ.get("ES_INDEX_REVIEWS", "agent-review-records")
PROV = os.environ.get("ES_INDEX_PROVENANCE", "agent-provenance-records")

if not ES_API_KEY:
    raise SystemExit("ES_API_KEY is required")

ctx = None
if not ES_VERIFY_TLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

def search(index, body):
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_search",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"ApiKey {ES_API_KEY}"},
        method="GET",
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode())

active = search(TASKS, {
    "size": 20,
    "query": {"terms": {"status": ["inbox", "planned", "ready", "running", "review", "blocked"]}},
    "sort": [{"updated_at": {"order": "desc", "unmapped_type": "date"}}]
})
failures = search(FAILS, {
    "size": 10,
    "sort": [{"updated_at": {"order": "desc", "unmapped_type": "date"}}]
})
reviews = search(REVIEWS, {
    "size": 10,
    "sort": [{"created_at": {"order": "desc", "unmapped_type": "date"}}]
})
prov = search(PROV, {
    "size": 10,
    "sort": [{"created_at": {"order": "desc", "unmapped_type": "date"}}]
})
print(json.dumps({
    "active_tasks": active.get("hits", {}).get("hits", []),
    "recent_failures": failures.get("hits", {}).get("hits", []),
    "recent_reviews": reviews.get("hits", {}).get("hits", []),
    "recent_provenance": prov.get("hits", {}).get("hits", []),
}, indent=2))
