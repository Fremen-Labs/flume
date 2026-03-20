#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_OAUTH_PY="${SCRIPT_DIR}/codex_oauth_login.py"

# Flume repo / package root (NOT install/ — fixes .env and state file paths).
detect_flume_root() {
  if [ -n "${FLUME_ROOT:-}" ]; then
    cd "${FLUME_ROOT}" && pwd
    return
  fi
  if [ -d "${SCRIPT_DIR}/../../src/dashboard" ] || [ -d "${SCRIPT_DIR}/../../src" ]; then
    cd "${SCRIPT_DIR}/../.." && pwd
    return
  fi
  cd "${SCRIPT_DIR}/.." && pwd
}

FLUME_ROOT="$(detect_flume_root)"
ENV_FILE="${FLUME_ROOT}/.env"

if [ -n "${OPENAI_OAUTH_STATE_FILE:-}" ]; then
  if [[ "${OPENAI_OAUTH_STATE_FILE}" = /* ]]; then
    STATE_FILE="${OPENAI_OAUTH_STATE_FILE}"
  else
    STATE_FILE="${FLUME_ROOT}/${OPENAI_OAUTH_STATE_FILE}"
  fi
else
  STATE_FILE="${FLUME_ROOT}/.openai-oauth.json"
fi

TOKEN_URL="${OPENAI_OAUTH_TOKEN_URL:-https://auth.openai.com/oauth/token}"
OPENCLAW_DIR_DEFAULT="${HOME}/.open""claw"
OPENCLAW_AUTH_DEFAULT="${OPENCLAW_AUTH_FILE:-${OPENCLAW_DIR_DEFAULT}/agents/main/agent/auth-profiles.json}"
PROFILE_DEFAULT="${OPENCLAW_AUTH_PROFILE:-openai-codex:default}"
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"

usage() {
    cat <<EOF
OpenAI ChatGPT / Codex OAuth helper for Flume (Flume root: ${FLUME_ROOT})

Prefer the Flume CLI from this directory:
  ./flume codex-oauth login | import | bootstrap | refresh | status

Direct script usage:
  bash install/setup/openai-oauth.sh login
  bash install/setup/openai-oauth.sh import-codex [codex_home]
  bash install/setup/openai-oauth.sh bootstrap
  bash install/setup/openai-oauth.sh import-openclaw [auth_file] [profile]
  bash install/setup/openai-oauth.sh refresh
  bash install/setup/openai-oauth.sh status

Commands:
  login            Device-code login (same flow as: codex login --device-auth). Updates .openai-oauth.json + .env.
  import-codex     Import tokens from official Codex CLI (${CODEX_HOME}/auth.json).
  bootstrap        Try import-codex first, then optional legacy profile import; refresh + sync .env.
  import-openclaw  Copy oauth from a third-party auth-profiles.json (advanced).
  refresh          Refresh access token from saved refresh token.
  status           Show token metadata (never prints full tokens).

Environment:
  FLUME_ROOT                  Override Flume repository / package root directory
  OPENAI_OAUTH_STATE_FILE     Override state file path
  OPENAI_OAUTH_CLIENT_ID      Advanced: override OAuth client id (default matches Codex CLI)
EOF
}

import_openclaw() {
    local auth_file="${1:-$OPENCLAW_AUTH_DEFAULT}"
    local profile="${2:-$PROFILE_DEFAULT}"
    python3 - "$auth_file" "$profile" "$STATE_FILE" <<'PY'
import json, sys, base64
from pathlib import Path

auth_file = Path(sys.argv[1])
profile = sys.argv[2]
state_file = Path(sys.argv[3])
if not auth_file.exists():
    raise SystemExit(f"OpenClaw auth file not found: {auth_file}")

data = json.loads(auth_file.read_text())
p = ((data.get("profiles") or {}).get(profile) or {})
if not p:
    raise SystemExit(f"Profile not found: {profile}")
access = str(p.get("access") or "").strip()
refresh = str(p.get("refresh") or "").strip()
expires = int(p.get("expires") or 0)
if not refresh:
    raise SystemExit("No refresh token found in OpenClaw profile.")

client_id = ""
if access.count(".") >= 2:
    try:
        payload = access.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        obj = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        client_id = str(obj.get("client_id") or "")
    except Exception:
        client_id = ""

state = {
    "provider": "openai-oauth",
    "profile": profile,
    "access": access,
    "refresh": refresh,
    "expires": expires,
    "client_id": client_id,
    "token_url": "https://auth.openai.com/oauth/token",
}
state_file.parent.mkdir(parents=True, exist_ok=True)
state_file.write_text(json.dumps(state, indent=2))
print(f"Wrote OAuth state: {state_file}")
print(f"Client ID present: {'yes' if client_id else 'no'}")
PY
}

refresh_token() {
    python3 - "$STATE_FILE" "$TOKEN_URL" <<'PY'
import json, sys, urllib.request
from pathlib import Path
import time

state_path = Path(sys.argv[1])
token_url = sys.argv[2]
if not state_path.exists():
    raise SystemExit(f"State file not found: {state_path}")

state = json.loads(state_path.read_text())
refresh = str(state.get("refresh") or "").strip()
client_id = str(state.get("client_id") or "").strip()
if not refresh or not client_id:
    raise SystemExit("State file must contain refresh + client_id.")

payload = {
    "grant_type": "refresh_token",
    "refresh_token": refresh,
    "client_id": client_id,
}
req = urllib.request.Request(
    token_url,
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read().decode())

access = str(data.get("access_token") or "").strip()
if not access:
    raise SystemExit("Refresh succeeded but no access_token returned.")

state["access"] = access
if data.get("refresh_token"):
    state["refresh"] = data["refresh_token"]
expires_in = int(data.get("expires_in") or 0)
if expires_in > 0:
    state["expires"] = int(time.time() * 1000) + expires_in * 1000
state["token_url"] = token_url
state_path.write_text(json.dumps(state, indent=2))
print(f"Refreshed access token. State updated: {state_path}")
PY
}

sync_env() {
    python3 - "$ENV_FILE" "$STATE_FILE" <<'PY'
import json, sys
from pathlib import Path

env_path = Path(sys.argv[1])
state_path = Path(sys.argv[2])
if not env_path.exists():
    raise SystemExit(f".env not found: {env_path}")
if not state_path.exists():
    raise SystemExit(f"State file not found: {state_path}")

state = json.loads(state_path.read_text())
access = str(state.get("access") or "").strip()
if not access:
    raise SystemExit("No access token in state file.")

lines = env_path.read_text().splitlines()
updates = {
    "LLM_PROVIDER": "openai",
    "LLM_API_KEY": access,
    "OPENAI_OAUTH_STATE_FILE": str(state_path.resolve()),
    "OPENAI_OAUTH_TOKEN_URL": str(state.get("token_url") or "https://auth.openai.com/oauth/token"),
}
seen = set()
out = []
for line in lines:
    if "=" not in line or line.strip().startswith("#"):
        out.append(line)
        continue
    key = line.split("=", 1)[0].strip()
    if key in updates:
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for k, v in updates.items():
    if k not in seen:
        out.append(f"{k}={v}")
env_path.write_text("\n".join(out) + "\n")
print(f"Updated .env: {env_path}")
PY
}

status() {
    python3 - "$STATE_FILE" <<'PY'
import json, sys, time
from pathlib import Path

state_path = Path(sys.argv[1])
if not state_path.exists():
    raise SystemExit(f"State file not found: {state_path}")
state = json.loads(state_path.read_text())
exp = int(state.get("expires") or 0)
if exp:
    remaining = int((exp - int(time.time() * 1000)) / 1000)
else:
    remaining = 0
print(f"State file: {state_path}")
print(f"Client ID: {str(state.get('client_id') or '')[:16]}...")
print(f"Refresh token present: {'yes' if state.get('refresh') else 'no'}")
print(f"Access token present: {'yes' if state.get('access') else 'no'}")
print(f"Expires in (seconds): {remaining}")
PY
}

cmd="${1:-}"
case "$cmd" in
    login)
        [ -f "${CODEX_OAUTH_PY}" ] || { echo "Missing ${CODEX_OAUTH_PY}" >&2; exit 1; }
        python3 "${CODEX_OAUTH_PY}" login --flume-root "${FLUME_ROOT}"
        ;;
    import-codex)
        [ -f "${CODEX_OAUTH_PY}" ] || { echo "Missing ${CODEX_OAUTH_PY}" >&2; exit 1; }
        python3 "${CODEX_OAUTH_PY}" import-codex --codex-home "${2:-${CODEX_HOME}}" --flume-root "${FLUME_ROOT}"
        ;;
    bootstrap)
        if [ -f "${CODEX_OAUTH_PY}" ] && [ -f "${CODEX_HOME}/auth.json" ]; then
            echo "Using official Codex CLI session at ${CODEX_HOME}/auth.json"
            python3 "${CODEX_OAUTH_PY}" import-codex --codex-home "${CODEX_HOME}" --flume-root "${FLUME_ROOT}"
            refresh_token
            sync_env
            echo "Bootstrap complete. Restart Flume dashboard/workers."
            exit 0
        fi
        auth_try="${2:-$OPENCLAW_AUTH_DEFAULT}"
        if [ -f "${auth_try}" ] && import_openclaw "${auth_try}" "${3:-$PROFILE_DEFAULT}"; then
            refresh_token
            sync_env
            echo "Bootstrap complete (OpenClaw). Restart Flume dashboard/workers."
            exit 0
        fi
        echo "No usable OAuth source found."
        echo "  • Recommended:  ./flume codex-oauth login"
        echo "  • Or:  codex login  then  ./flume codex-oauth import"
        echo "  • Or:  python3 install/setup/codex_oauth_login.py login --flume-root ${FLUME_ROOT}"
        echo "  • Advanced legacy import:  bash install/setup/openai-oauth.sh import-openclaw"
        exit 1
        ;;
    import-openclaw)
        import_openclaw "${2:-$OPENCLAW_AUTH_DEFAULT}" "${3:-$PROFILE_DEFAULT}"
        ;;
    refresh)
        refresh_token
        sync_env
        ;;
    status)
        status
        ;;
    -h|--help|help|"")
        usage
        ;;
    *)
        echo "Unknown command: $cmd" >&2
        usage
        exit 1
        ;;
esac
