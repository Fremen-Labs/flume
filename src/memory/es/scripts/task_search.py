#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
import urllib.error
import ssl

ES_URL = os.environ.get("ES_URL", "https://localhost:9200").rstrip("/")
ES_API_KEY = os.environ.get("ES_API_KEY")
ES_VERIFY_TLS = os.environ.get("ES_VERIFY_TLS", "false").lower() == "true"
INDEX = os.environ.get("ES_INDEX_TASKS", "agent-task-records")

if not ES_API_KEY:
    raise SystemExit("ES_API_KEY is required")
query = " ".join(sys.argv[1:]).strip()
if not query:
    raise SystemExit("usage: task_search.py <query>")

ctx = None
if not ES_VERIFY_TLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

headers = {"Content-Type": "application/json", "Authorization": f"ApiKey {ES_API_KEY}"}

# Fast path: stable task document id lookup
if " " not in query:
    try:
        req = urllib.request.Request(f"{ES_URL}/{INDEX}/_doc/{query}", headers=headers, method="GET")
        with urllib.request.urlopen(req, context=ctx) as resp:
            data = json.loads(resp.read().decode())
        if data.get("found"):
            print(json.dumps({
                "hits": {
                    "total": {"value": 1, "relation": "eq"},
                    "hits": [{
                        "_index": data.get("_index"),
                        "_id": data.get("_id"),
                        "_source": data.get("_source", {})
                    }]
                }
            }))
            raise SystemExit(0)
    except urllib.error.HTTPError:
        pass

body = {
    "size": 10,
    "query": {
        "multi_match": {
            "query": query,
            "fields": ["id^4", "title^3", "objective^2", "repo^2", "owner", "status"]
        }
    },
    "sort": [
        {"_score": "desc"},
        {"updated_at": {"order": "desc", "unmapped_type": "date"}},
        {"created_at": {"order": "desc", "unmapped_type": "date"}}
    ]
}
req = urllib.request.Request(
    f"{ES_URL}/{INDEX}/_search",
    data=json.dumps(body).encode(),
    headers=headers,
    method="GET",
)
with urllib.request.urlopen(req, context=ctx) as resp:
    print(resp.read().decode())
