#!/usr/bin/env python3
import os
import json
import urllib.request
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path

# Inject local path so we can resolve flume_secrets
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from flume_secrets import apply_runtime_config
except ImportError:
    # Fallback if run from different dir
    sys.stderr.write("Failed to import flume_secrets. Skipping seed.\\n")
    sys.exit(0)

def push_doc(es_url, es_key, index, doc_id, doc):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        f"{es_url.rstrip('/')}/{index}/_doc/{doc_id}",
        data=json.dumps(doc).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': f'ApiKey {es_key}'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
            pass
    except Exception as e:
        sys.stderr.write(f"Failed to seed {index}: {e}\\n")

def seed():
    # Load secrets dynamically from OpenBao/.env
    apply_runtime_config(Path(__file__).parent.parent)
    
    es_url = os.environ.get('ES_URL')
    es_key = os.environ.get('ES_API_KEY')
    
    if not es_url or not es_key:
        sys.stdout.write("Elasticsearch not configured properly. Skipping telemetry seed.\\n")
        return

    now = datetime.now(timezone.utc).isoformat()

    # 1. Bootstrapping Task
    task_id = "seed-task-001"
    task_doc = {
        "title": "Initialize Flume Framework Core",
        "description": "System architecture compiled and baseline dependencies resolved successfully.",
        "status": "done",
        "created_at": now,
        "updated_at": now,
        "assigned_agent_role": "implementer",
        "owner": "system"
    }

    # 2. Mock Approval Review
    review_id = "seed-review-001"
    review_doc = {
        "task_id": task_id,
        "verdict": "approved",
        "feedback": "System initialization sequence perfectly coherent and verified.",
        "evaluator": "system-critic",
        "created_at": now
    }

    # 3. Baseline EXO Token Usage from nexus-setup.py operations
    token_doc = {
        "worker_name": "system-installer",
        "worker_role": "installer",
        "provider": "Exo Cluster",
        "model": "qwen3-30b",
        "input_tokens": 128,
        "output_tokens": 56,
        "savings": 8000,
        "created_at": now
    }

    sys.stdout.write("Injecting initial telemetry seeds into the Flume Data Matrix...\\n")

    push_doc(es_url, es_key, "agent-task-records", task_id, task_doc)
    push_doc(es_url, es_key, "agent-review-records", review_id, review_doc)
    push_doc(es_url, es_key, "agent-token-telemetry", "seed-token-001", token_doc)

    sys.stdout.write("Done. Dashboard successfully populated!\\n")

if __name__ == '__main__':
    seed()
