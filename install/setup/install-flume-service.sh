#!/usr/bin/env bash
# Install Flume dashboard as a systemd user service
# Runs in background, starts on login (if enabled).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [ -d "${INSTALL_DIR}/../src" ]; then
    FLUME_ROOT="$(cd "${INSTALL_DIR}/.." && pwd)"
    DASHBOARD_SCRIPT="${FLUME_ROOT}/src/dashboard/run.sh"
else
    FLUME_ROOT="${INSTALL_DIR}"
    DASHBOARD_SCRIPT="${FLUME_ROOT}/dashboard/run.sh"
fi
ENV_FILE="${FLUME_ROOT}/.env"

if [ ! -f "${ENV_FILE}" ]; then
    echo "Error: .env not found at ${ENV_FILE}. Run setup.sh first."
    exit 1
fi

if [ ! -f "${DASHBOARD_SCRIPT}" ]; then
    echo "Error: Dashboard script not found at ${DASHBOARD_SCRIPT}"
    exit 1
fi

UNIT_DIR="${HOME}/.config/systemd/user"
if [ -n "${SUDO_USER:-}" ]; then
    USER_HOME=$(getent passwd "${SUDO_USER}" | cut -d: -f6)
    UNIT_DIR="${USER_HOME}/.config/systemd/user"
fi

mkdir -p "${UNIT_DIR}"
SERVICE_FILE="${UNIT_DIR}/flume-dashboard.service"

sed -e "s|__FLUME_ROOT__|${FLUME_ROOT}|g" \
    -e "s|__DASHBOARD_SCRIPT__|${DASHBOARD_SCRIPT}|g" \
    -e "s|__ENV_FILE__|${ENV_FILE}|g" \
    "${SCRIPT_DIR}/flume-dashboard.service.template" > "${SERVICE_FILE}"

# systemd user services need EnvironmentFile to be loaded; we also need LOOM_WORKSPACE etc.
# The run.sh sources .env and sets LOOM_WORKSPACE. But ExecStart runs the script directly.
# The run.sh handles env loading. Remove EnvironmentFile if it causes issues - run.sh loads it.
# Actually EnvironmentFile in systemd loads vars - but run.sh sources .env itself. Let me keep it
# for any vars systemd might need. Actually the run.sh does the full setup. So we just need
# to run the script. The WorkingDirectory and ExecStart are enough. Let me simplify - remove
# EnvironmentFile from the template since run.sh loads everything. Or keep it for transparency.
# I'll keep it - it preloads the env for the process.

echo "Installed: ${SERVICE_FILE}"
echo ""
echo "To start the dashboard as a background service:"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user start flume-dashboard"
echo ""
echo "To start automatically on login:"
echo "  systemctl --user enable flume-dashboard"
echo ""
echo "Or use the flume CLI:  flume start"
