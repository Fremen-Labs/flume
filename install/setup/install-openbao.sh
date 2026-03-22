#!/usr/bin/env bash
# Flume — OpenBao installer (CLI binary)
#
# Installs the OpenBao CLI by downloading the latest (or requested) Linux
# release artifact from GitHub and placing the binary in /usr/local/bin/openbao.
# Upstream ships the binary as "bao"; Flume expects the command "openbao".
#
# Usage:
#   sudo bash setup/install-openbao.sh
#   OPENBAO_VERSION=v2.5.1 sudo bash setup/install-openbao.sh

set -euo pipefail

UNAME_S="$(uname -s 2>/dev/null || echo unknown)"
if [ "$UNAME_S" != "Linux" ]; then
    echo "This helper currently supports Linux only. On macOS, use Docker or install/configure the service manually." >&2
    exit 1
fi

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

# sudo often uses secure_path without /usr/local/bin; we install there anyway.
export PATH="/usr/local/bin:/usr/local/sbin:${PATH}"

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

# Upstream CLI is often installed as "bao"; Flume calls "openbao"
if command -v bao >/dev/null 2>&1; then
    BAO_PATH="$(command -v bao)"
    info "Linking /usr/local/bin/openbao -> ${BAO_PATH}"
    ln -sf "${BAO_PATH}" /usr/local/bin/openbao
    success "openbao command available (symlink to bao)"
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

with urllib.request.urlopen(api_url, timeout=30) as r:
    data = json.load(r)

assets = data.get("assets", [])
candidates = []

def arch_matches(filename: str) -> bool:
    """OpenBao uses linux_amd64, Linux_x86_64, linux_arm64, Linux_arm64, etc."""
    f = filename.lower()
    if arch == "amd64":
        return "amd64" in f or "x86_64" in f
    if arch == "arm64":
        return "arm64" in f
    return False

for a in assets:
    name = a.get("name") or ""
    url = a.get("browser_download_url") or ""
    if not url:
        continue
    n = name.lower()
    if "linux" not in n:
        continue
    if not arch_matches(name):
        continue
    if not (n.endswith(".zip") or n.endswith(".tar.gz")):
        continue
    if "sbom" in n or "gpgsig" in n or "sigstore" in n:
        continue
    # Prefer standard CLI bundle over HSM-specific builds
    penalty = 0
    if "bao-hsm" in n:
        penalty += 100
    if n.endswith(".zip"):
        penalty += 1
    candidates.append((penalty, len(name), name, url))

if not candidates:
    print("", end="")
    sys.exit(0)

candidates.sort()
print(candidates[0][3], end="")
PY
)"

if [ -z "${ASSET_URL}" ]; then
    error "Could not find a matching OpenBao Linux ${ARCH} release artifact."
fi

ASSET_FILE="${TMP_DIR}/openbao-archive"
info "Downloading: ${ASSET_URL}"
curl -fL "${ASSET_URL}" -o "${ASSET_FILE}"

case "${ASSET_URL}" in
    *.zip) FILE_TYPE="zip" ;;
    *)     FILE_TYPE="tgz" ;;
esac

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

BIN_PATH="$(
python3 - << 'PY' "${TMP_DIR}"
import os
import sys

root = sys.argv[1]
# Prefer openbao, then bao (upstream name)
found = []
for base, _, files in os.walk(root):
    for fname in files:
        if fname not in ("openbao", "bao"):
            continue
        p = os.path.join(base, fname)
        if os.path.isfile(p):
            found.append(p)

def key(p):
    base = os.path.basename(p)
    return (0 if base == "openbao" else 1, len(p))

if not found:
    sys.exit(0)
found.sort(key=key)
print(found[0], end="")
PY
)"

if [ -z "${BIN_PATH}" ] || [ ! -f "${BIN_PATH}" ]; then
    error "Downloaded artifact did not contain an openbao or bao binary."
fi

OPENBAO_INSTALL_BIN="/usr/local/bin/openbao"
install -m 0755 "${BIN_PATH}" "${OPENBAO_INSTALL_BIN}"
success "Installed OpenBao to ${OPENBAO_INSTALL_BIN} (from upstream binary: $(basename "${BIN_PATH}"))"

if [ -x "${OPENBAO_INSTALL_BIN}" ]; then
    info "OpenBao version:"
    "${OPENBAO_INSTALL_BIN}" version || true
else
    error "Installation completed but ${OPENBAO_INSTALL_BIN} is missing or not executable."
fi
