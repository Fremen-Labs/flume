#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
import ssl
import tempfile
import subprocess

ES_URL = os.environ.get("ES_URL", "https://localhost:9200").rstrip("/")
ES_API_KEY = os.environ.get("ES_API_KEY")
ES_VERIFY_TLS = os.environ.get("ES_VERIFY_TLS", "false").lower() == "true"
INDEX = os.environ.get("ES_INDEX_MEMORY", "agent-memory-entries")

if not ES_API_KEY:
    raise SystemExit("ES_API_KEY is required")
if len(sys.argv) < 3:
    raise SystemExit("usage: promote_from_search.py <query> <target_markdown_file>")

query = sys.argv[1]
target = sys.argv[2]

ctx = None
if not ES_VERIFY_TLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

body = {
    "size": 1,
    "query": {
        "multi_match": {
            "query": query,
            "fields": ["title^3", "summary^2", "statement", "tags^2"]
        }
    },
    "sort": [{"_score": "desc"}, {"updated_at": {"order": "desc", "unmapped_type": "date"}}]
}
req = urllib.request.Request(
    f"{ES_URL}/{INDEX}/_search",
    data=json.dumps(body).encode(),
    headers={"Content-Type":"application/json","Authorization":f"ApiKey {ES_API_KEY}"},
    method="GET",
)
with urllib.request.urlopen(req, context=ctx) as resp:
    data = json.loads(resp.read().decode())

hits = data.get("hits", {}).get("hits", [])
if not hits:
    raise SystemExit("No memory hit found")
entry = hits[0]["_source"]
with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
    json.dump(entry, f)
    tmp = f.name
subprocess.check_call([str(os.path.join(os.path.dirname(__file__), "promote_memory.py")), tmp, target])
print(json.dumps(entry, indent=2))
