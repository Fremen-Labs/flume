import time
import json
import urllib.request
import urllib.error
import ssl
import subprocess
from datetime import datetime

ES_PASS = "flume_es_504a20fe51c8c6d5c5abf3570e487093"
ES_URL = "https://localhost:9200"

# Setup SSL context to ignore self-signed certs
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def query_es(index):
    url = f"{ES_URL}/{index}/_search?size=100"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic ZWxhc3RpYzpmbHVtZV9lc181MDRhMjBmZTUxYzhjNmQ1YzVhYmYzNTcwZTQ4NzA5Mw==" # elastic:password base64
    })
    try:
        with urllib.request.urlopen(req, context=ctx) as response:
            data = json.loads(response.read().decode())
            hits = data.get("hits", {}).get("hits", [])
            return [h["_source"] for h in hits]
    except Exception as e:
        print(f"Error querying {index}: {e}")
        return []

def get_docker_logs(container, since_secs=3):
    try:
        res = subprocess.run(
            ["docker", "compose", "logs", "--since", f"{since_secs}s", container],
            capture_output=True,
            text=True,
            cwd="/Users/jonathandoughty/clients/fremenlabs/flume /flume"
        )
        return res.stdout.strip()
    except Exception as e:
        return f"Error getting logs: {e}"

print("Flume Test Monitor Started...")
last_plans = {}
last_tasks = {}

with open("/Users/jonathandoughty/clients/fremenlabs/flume /flume/monitoring.log", "w") as log_file:
    log_file.write("--- Flume Monitor Started at " + datetime.now().isoformat() + " ---\n")
    log_file.flush()

while True:
    try:
        # Check plans
        plans = query_es("agent-plan-sessions")
        for p in plans:
            pid = p.get("id") or p.get("session_id")
            # Log any new plans or changes
            p_str = json.dumps(p)
            if pid not in last_plans or last_plans[pid] != p_str:
                msg = f"[{datetime.now().isoformat()}] PLAN SESSION CHANGE: {p_str}\n"
                print(msg.strip())
                with open("/Users/jonathandoughty/clients/fremenlabs/flume /flume/monitoring.log", "a") as f:
                    f.write(msg)
                    f.flush()
                last_plans[pid] = p_str

        # Check tasks
        tasks = query_es("agent-task-records")
        for t in tasks:
            tid = t.get("id")
            t_str = json.dumps(t)
            if tid not in last_tasks or last_tasks[tid] != t_str:
                old_status = json.loads(last_tasks[tid]).get("status") if tid in last_tasks else "NEW"
                new_status = t.get("status")
                msg = f"[{datetime.now().isoformat()}] TASK {tid} ({t.get('title')}): {old_status} -> {new_status} (Queue: {t.get('queue_state')})\n"
                print(msg.strip())
                with open("/Users/jonathandoughty/clients/fremenlabs/flume /flume/monitoring.log", "a") as f:
                    f.write(msg)
                    f.write(f"  Full details: {t_str}\n")
                    f.flush()
                last_tasks[tid] = t_str

        # Capture container logs
        for service in ["dashboard", "gateway", "worker"]:
            logs = get_docker_logs(service, since_secs=2)
            if logs:
                with open("/Users/jonathandoughty/clients/fremenlabs/flume /flume/monitoring.log", "a") as f:
                    for line in logs.splitlines():
                        if "WARN[0000]" not in line and "variable is not set" not in line:
                            f.write(f"[{datetime.now().isoformat()}] [{service.upper()}] {line}\n")
                    f.flush()

    except Exception as e:
        print(f"Loop error: {e}")
    time.sleep(2)
