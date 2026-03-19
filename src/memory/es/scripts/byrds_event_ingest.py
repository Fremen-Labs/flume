#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit("usage: byrds_event_ingest.py <event_json_file>")

event = json.loads(Path(sys.argv[1]).read_text())
scripts = Path(__file__).resolve().parent
etype = event.get("event_type")
env = os.environ.copy()
load_env = scripts / "load_env.sh"

def run_bash(command: str):
    subprocess.check_call(["bash", "-lc", f"source {load_env} && {command}"], env=env)

if etype == "task.created":
    run_bash(" ".join([
        str(scripts / "task_create.sh"),
        json.dumps(event["task_id"]),
        json.dumps(event.get("title", event["task_id"])),
        json.dumps(event.get("objective", event.get("summary", ""))),
        json.dumps(event.get("repo", "workspace")),
        json.dumps(event.get("role", "planner")),
        json.dumps(event.get("priority", "normal")),
        json.dumps(event.get("risk", "medium")),
    ]))
elif etype in {"task.updated", "task.status_changed"}:
    frag = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json")
    json.dump({
        "status": event.get("status", "planned"),
        "owner": event.get("role", "planner"),
        "needs_human": event.get("needs_human", False),
        "priority": event.get("priority", "normal"),
    }, frag)
    frag.close()
    run_bash(f"{scripts / 'task_update_by_key.py'} {json.dumps(event['task_id'])} {json.dumps(frag.name)}")
elif etype == "task.handoff":
    run_bash(" ".join([
        str(scripts / "write_handoff.sh"),
        json.dumps(event["task_id"]),
        json.dumps(event.get("from_role", "planner")),
        json.dumps(event.get("to_role", "implementer")),
        json.dumps(event.get("reason", event.get("summary", "handoff"))),
        json.dumps(event.get("objective", event.get("summary", ""))),
        json.dumps(event.get("status", "ready")),
    ]))
else:
    raise SystemExit(f"Unhandled Byrds event_type: {etype}")

print(json.dumps({"ok": True, "event_type": etype}))
