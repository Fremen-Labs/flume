#!/usr/bin/env bash
# Flume — GitHub CLI (gh) installer
#
# Installs the gh binary from the latest (or requested) GitHub CLI release
# into /usr/local/bin/gh.
#
# Usage:
#   sudo bash setup/install-gh.sh
#   GH_CLI_VERSION=v2.88.1 sudo bash setup/install-gh.sh

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
    error "python3 is required to resolve/download gh release assets."
fi

if ! command -v curl >/dev/null 2>&1; then
    error "curl is required to download gh."
fi

if command -v gh >/dev/null 2>&1; then
    warn "gh already installed: $(gh --version 2>/dev/null | head -n 1 || echo gh)"
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

REQUESTED_VERSION="${GH_CLI_VERSION:-latest}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

info "Resolving GitHub CLI release artifact for linux/${ARCH}..."

if [ "${REQUESTED_VERSION}" = "latest" ]; then
    RELEASE_API_URL="https://api.github.com/repos/cli/cli/releases/latest"
else
    RELEASE_API_URL="https://api.github.com/repos/cli/cli/releases/tags/${REQUESTED_VERSION}"
fi

ASSET_URL="$(
python3 - << 'PY' "${RELEASE_API_URL}" "${ARCH}"
import json
import sys
import urllib.request

api_url = sys.argv[1]
arch = sys.argv[2]

with urllib.request.urlopen(api_url, timeout=30) as r:
    data = json.load(r)

assets = data.get("assets", [])
candidates = []
for a in assets:
    name = (a.get("name") or "").lower()
    url = a.get("browser_download_url") or ""
    if not url:
        continue
    if not name.endswith(".tar.gz"):
        continue
    if f"linux_{arch}" not in name:
        continue
    if not name.startswith("gh_"):
        continue
    if "sbom" in name or "gpgsig" in name or "sigstore" in name:
        continue
    candidates.append((name, url))

if not candidates:
    print("", end="")
    sys.exit(0)

# Prefer shortest name (avoids odd variants)
candidates.sort(key=lambda x: len(x[0]))
print(candidates[0][1], end="")
PY
)"

if [ -z "${ASSET_URL}" ]; then
    error "Could not find a matching gh Linux ${ARCH} .tar.gz release artifact."
fi

ASSET_FILE="${TMP_DIR}/gh-archive.tgz"
info "Downloading: ${ASSET_URL}"
curl -fL "${ASSET_URL}" -o "${ASSET_FILE}"

tar -xzf "${ASSET_FILE}" -C "${TMP_DIR}"

BIN_PATH="$(
python3 - << 'PY' "${TMP_DIR}"
import os
import sys

root = sys.argv[1]
for base, _, files in os.walk(root):
    if "gh" in files:
        p = os.path.join(base, "gh")
        if os.path.isfile(p) and os.access(p, os.X_OK):
            print(p, end="")
            raise SystemExit(0)
        if os.path.isfile(p):
            print(p, end="")
            raise SystemExit(0)
PY
)"

if [ -z "${BIN_PATH}" ] || [ ! -f "${BIN_PATH}" ]; then
    error "Downloaded archive did not contain a gh binary."
fi

install -m 0755 "${BIN_PATH}" /usr/local/bin/gh
success "Installed GitHub CLI to /usr/local/bin/gh"

if command -v gh >/dev/null 2>&1; then
    info "gh version:"
    gh --version || true
else
    error "Installation completed but gh is not on PATH."
fi
