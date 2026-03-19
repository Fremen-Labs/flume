#!/usr/bin/env bash
# Flume — OpenBao installer (CLI binary)
#
# Installs the OpenBao CLI by downloading the latest (or requested) Linux
# release artifact from GitHub and placing the `openbao` binary in
# /usr/local/bin/openbao.
#
# Usage:
#   sudo bash setup/install-openbao.sh
#   OPENBAO_VERSION=v2.2.0 sudo bash setup/install-openbao.sh

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

if [ "${EUID}" -ne 0 ]; then
    error "This script must be run as root (use: sudo $0)"
fi

if ! command -v python3 >/dev/null 2>&1; then
    error "python3 is required to resolve/download OpenBao release assets."
fi

if ! command -v curl >/dev/null 2>&1; then
    error "curl is required to download OpenBao."
fi

if command -v openbao >/dev/null 2>&1; then
    CURRENT_VER="$(openbao version 2>/dev/null | head -n 1 || true)"
    warn "OpenBao already appears installed: ${CURRENT_VER:-unknown version}"
    exit 0
fi

ARCH_RAW="$(uname -m)"
case "${ARCH_RAW}" in
    x86_64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *)
        error "Unsupported architecture: ${ARCH_RAW} (supported: x86_64, aarch64/arm64)"
        ;;
esac

REQUESTED_VERSION="${OPENBAO_VERSION:-latest}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

info "Resolving OpenBao release artifact for linux/${ARCH}..."

if [ "${REQUESTED_VERSION}" = "latest" ]; then
    RELEASE_API_URL="https://api.github.com/repos/openbao/openbao/releases/latest"
else
    RELEASE_API_URL="https://api.github.com/repos/openbao/openbao/releases/tags/${REQUESTED_VERSION}"
fi

ASSET_URL="$(
python3 - << 'PY' "${RELEASE_API_URL}" "${ARCH}"
import json
import sys
import urllib.request

api_url = sys.argv[1]
arch = sys.argv[2]

with urllib.request.urlopen(api_url, timeout=20) as r:
    data = json.load(r)

assets = data.get("assets", [])
candidates = []
for a in assets:
    name = (a.get("name") or "").lower()
    url = a.get("browser_download_url") or ""
    if "linux" not in name:
        continue
    if arch not in name:
        continue
    if not (name.endswith(".zip") or name.endswith(".tar.gz")):
        continue
    candidates.append((name, url))

if not candidates:
    print("", end="")
    sys.exit(0)

# Prefer zip first, then tar.gz.
def score(item):
    name, _ = item
    return (0 if name.endswith(".zip") else 1, len(name))

candidates.sort(key=score)
print(candidates[0][1], end="")
PY
)"

if [ -z "${ASSET_URL}" ]; then
    error "Could not find a matching OpenBao Linux ${ARCH} release artifact."
fi

ASSET_FILE="${TMP_DIR}/openbao-archive"
info "Downloading: ${ASSET_URL}"
curl -fL "${ASSET_URL}" -o "${ASSET_FILE}"

BIN_PATH=""
if python3 - << 'PY' "${ASSET_URL}"
import sys
print("zip" if sys.argv[1].lower().endswith(".zip") else "other", end="")
PY
then
    FILE_TYPE="$(python3 - << 'PY' "${ASSET_URL}"
import sys
print("zip" if sys.argv[1].lower().endswith(".zip") else "other", end="")
PY
)"
else
    FILE_TYPE="other"
fi

if [ "${FILE_TYPE}" = "zip" ]; then
    python3 - << 'PY' "${ASSET_FILE}" "${TMP_DIR}"
import sys
import zipfile

archive = sys.argv[1]
out_dir = sys.argv[2]
with zipfile.ZipFile(archive, "r") as z:
    z.extractall(out_dir)
PY
else
    tar -xzf "${ASSET_FILE}" -C "${TMP_DIR}"
fi

if [ -f "${TMP_DIR}/openbao" ]; then
    BIN_PATH="${TMP_DIR}/openbao"
else
    BIN_PATH="$(
python3 - << 'PY' "${TMP_DIR}"
import os
import sys

root = sys.argv[1]
for base, _, files in os.walk(root):
    if "openbao" in files:
        print(os.path.join(base, "openbao"), end="")
        break
PY
)"
fi

if [ -z "${BIN_PATH}" ] || [ ! -f "${BIN_PATH}" ]; then
    error "Downloaded artifact did not contain an openbao binary."
fi

install -m 0755 "${BIN_PATH}" /usr/local/bin/openbao
success "Installed OpenBao to /usr/local/bin/openbao"

if command -v openbao >/dev/null 2>&1; then
    info "OpenBao version:"
    openbao version || true
else
    error "Installation completed but openbao is not on PATH."
fi

