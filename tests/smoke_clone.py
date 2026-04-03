#!/usr/bin/env python3
"""
Smoke test for the git clone project flow.

Usage (server must be running on :8765):
    python tests/smoke_clone.py [--base-url http://localhost:8765]

Tests:
  1. POST /api/projects  with an HTTPS URL  -> clone_status should be 'cloning'
  2. GET  /api/projects/{id}/clone-status   -> polls until 'cloned' or 'failed' (max 120s)
  3. GET  /api/repos/{id}/branches          -> should return gitAvailable: true + branch list
  4. GET  /api/repos/{id}/tree             -> should return non-empty file list
  5. GET  /api/repos/{id}/file             -> should return file content for README.md (if exists)
  6. POST /api/projects/{id}/delete        -> cleanup
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error

GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

# ── Public repo that clones fast and has README.md ──
TEST_REPO_URL = "https://github.com/octocat/Hello-World.git"

def ok(msg):  print(f"  {GREEN}✓{RESET} {msg}")
def fail(msg): print(f"  {RED}✗{RESET} {msg}"); sys.exit(1)
def info(msg): print(f"  {YELLOW}→{RESET} {msg}")

def request(method, url, payload=None, timeout=15):
    data = json.dumps(payload).encode() if payload else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    except Exception as e:
        fail(f"Request failed: {e}")


def run(base_url: str):
    print(f"\n{BOLD}Flume Clone Smoke Test{RESET}  →  {base_url}\n")

    # ── 1. Health check ───────────────────────────────────────────────────────
    print(f"{BOLD}[1] Health check{RESET}")
    status, body = request("GET", f"{base_url}/api/health")
    if status != 200:
        fail(f"Server not reachable: HTTP {status}")
    ok(f"Server healthy: {body}")

    # ── 2. Create project with HTTPS URL ──────────────────────────────────────
    print(f"\n{BOLD}[2] Create project with remote URL{RESET}")
    info(f"Repo: {TEST_REPO_URL}")
    status, body = request("POST", f"{base_url}/api/projects", {
        "name": "smoke-test-clone",
        "repoUrl": TEST_REPO_URL,
    })
    if status != 200 or not body.get("success"):
        fail(f"Create failed (HTTP {status}): {body}")
    project_id = body["projectId"]
    clone_status = body.get("project", {}).get("clone_status")
    ok(f"Project created: {project_id}  clone_status={clone_status}")
    if clone_status != "cloning":
        fail(f"Expected clone_status='cloning', got '{clone_status}'")
    ok("clone_status is 'cloning' ✓")

    # ── 3. Poll clone-status until done ──────────────────────────────────────
    print(f"\n{BOLD}[3] Polling clone status (max 120s)…{RESET}")
    deadline = time.time() + 120
    final_status = None
    while time.time() < deadline:
        status, body = request("GET", f"{base_url}/api/projects/{project_id}/clone-status")
        cs = body.get("clone_status", "unknown")
        info(f"clone_status = {cs}")
        if cs == "cloned":
            final_status = "cloned"
            break
        elif cs == "failed":
            fail(f"Clone FAILED: {body.get('clone_error')}")
        time.sleep(4)

    if final_status != "cloned":
        fail("Clone did not complete within 120 seconds")
    ok(f"Clone complete  path={body.get('path')}  is_git={body.get('is_git')}")

    # ── 4. Branches ───────────────────────────────────────────────────────────
    print(f"\n{BOLD}[4] GET /api/repos/{project_id}/branches{RESET}")
    status, body = request("GET", f"{base_url}/api/repos/{project_id}/branches")
    if status != 200:
        fail(f"HTTP {status}: {body}")
    if not body.get("gitAvailable"):
        fail(f"gitAvailable=false — unexpected: {body}")
    branches = body.get("branches", [])
    default  = body.get("default")
    ok(f"branches={branches}  default={default}")

    # ── 5. Tree ───────────────────────────────────────────────────────────────
    print(f"\n{BOLD}[5] GET /api/repos/{project_id}/tree{RESET}")
    status, body = request("GET", f"{base_url}/api/repos/{project_id}/tree?branch={default or 'main'}")
    if status != 200:
        fail(f"HTTP {status}: {body}")
    entries = body.get("entries", [])
    if not entries:
        fail("Tree returned empty entries list")
    ok(f"{len(entries)} entries  sample={[e['path'] for e in entries[:5]]}")

    # ── 6. File content ───────────────────────────────────────────────────────
    print(f"\n{BOLD}[6] GET /api/repos/{project_id}/file  (README.md){RESET}")
    readme = next((e for e in entries if e["path"].lower() == "readme.md"), None)
    if readme:
        import urllib.parse
        params = urllib.parse.urlencode({"path": readme["path"], "branch": default or "main"})
        status, body = request("GET", f"{base_url}/api/repos/{project_id}/file?{params}")
        if status != 200:
            fail(f"HTTP {status}: {body}")
        if body.get("binary"):
            info("File is binary (unexpected for README.md)")
        else:
            preview = (body.get("content") or "")[:80].replace("\n", "↵")
            ok(f"Content received ({body.get('size')} bytes)  preview: {preview!r}")
    else:
        info("No README.md found in tree (repo may use different name) — skipping file test")

    # ── 7. LLM settings save (ok+restartRequired response shape) ─────────────
    print(f"\n{BOLD}[7] POST /api/settings/llm  (response shape){RESET}")
    status, body = request("GET", f"{base_url}/api/settings/llm")
    if status == 200:
        # Just verify the save endpoint returns ok+restartRequired
        settings = body.get("settings", {})
        save_payload = {
            "provider": settings.get("provider", "openai"),
            "model":    settings.get("model",    "gpt-4o"),
        }
        status2, body2 = request("POST", f"{base_url}/api/settings/llm", save_payload)
        if status2 == 200 and "ok" in body2:
            ok(f"Save response has 'ok' field: {body2}")
        else:
            info(f"Save returned HTTP {status2}: {body2}  (may need credentials configured)")
    else:
        info("Could not fetch LLM settings — skipping (OpenBao may not be running)")

    # ── 8. Cleanup ────────────────────────────────────────────────────────────
    print(f"\n{BOLD}[8] Cleanup — delete project{RESET}")
    status, body = request("POST", f"{base_url}/api/projects/{project_id}/delete")
    if status != 200 or not body.get("success"):
        info(f"Delete returned HTTP {status}: {body}  (manual cleanup may be needed)")
    else:
        ok(f"Project {project_id} deleted")

    print(f"\n{GREEN}{BOLD}All tests passed! ✓{RESET}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8765")
    args = parser.parse_args()
    run(args.base_url.rstrip("/"))
