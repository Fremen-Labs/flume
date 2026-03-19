#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit("usage: pipes_event_ingest.py <event_json_file>")

event = json.loads(Path(sys.argv[1]).read_text())
scripts = Path(__file__).resolve().parent
etype = event.get("event_type")
env = os.environ.copy()
load_env = scripts / "load_env.sh"

def run_bash(command: str):
    subprocess.check_call(["bash", "-lc", f"source {load_env} && {command}"], env=env)

def update_task(status, owner="executor", needs_human=False, priority="normal"):
    run_bash(" ".join([
        str(scripts / "task_update_by_key.sh"),
        json.dumps(event.get("task_id", "unknown-task")),
        json.dumps(status),
        json.dumps(owner),
        json.dumps(str(needs_human).lower()),
        json.dumps(priority),
    ]))

if etype in {"job.created", "job.started", "job.heartbeat", "job.retrying", "job.succeeded", "job.failed", "job.timed_out", "job.cancelled"}:
    run_bash(" ".join([
        str(scripts / "write_provenance.sh"),
        json.dumps(event.get("job_id", event.get("event_id", "job-event"))),
        json.dumps(event.get("task_id", "unknown-task")),
        json.dumps(event.get("project", "openclaw")),
        json.dumps(event.get("repo", "workspace")),
        json.dumps(event.get("role", "executor")),
        json.dumps(event.get("review_verdict", "pending")),
    ]))

    if etype == "job.started":
        update_task("running", owner=event.get("role", "executor"))
    elif etype == "job.succeeded":
        update_task("review", owner="reviewer")
    elif etype in {"job.failed", "job.timed_out"}:
        run_bash(" ".join([
            str(scripts / "write_failure.sh"),
            json.dumps(event.get("task_id", "unknown-task")),
            json.dumps(event.get("project", "openclaw")),
            json.dumps(event.get("repo", "workspace")),
            json.dumps(event.get("error_class", etype)),
            json.dumps(event.get("summary", etype)),
            json.dumps(event.get("root_cause", "")),
            json.dumps(event.get("fix_applied", "")),
        ]))
        update_task("blocked", owner=event.get("role", "executor"), needs_human=event.get("needs_human", False))
else:
    raise SystemExit(f"Unhandled Pipes event_type: {etype}")

print(json.dumps({"ok": True, "event_type": etype}))
