#!/usr/bin/env bash
# Flume — Node.js LTS (official binary) for npm / Codex CLI / frontend builds
#
# Installs Node + npm into /usr/local (same pattern as other Flume setup scripts).
# Skips if node is already >= 18.
#
# Usage:
#   sudo bash install/setup/install-nodejs-lts.sh
#
# Override version:
#   FLUME_NODEJS_LTS_VERSION=v22.14.0 sudo bash install/setup/install-nodejs-lts.sh

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
    error "Run as root: sudo $0"
fi

export PATH="/usr/local/bin:/usr/local/sbin:${PATH}"

NODE_OK=false
if command -v node >/dev/null 2>&1; then
    if node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' 2>/dev/null; then
        NODE_OK=true
    fi
fi

if [ "$NODE_OK" = "true" ]; then
    success "Node.js $(node --version) already satisfies >= 18 (skipping install)."
    exit 0
fi

if ! command -v curl >/dev/null 2>&1; then
    error "curl is required to download Node.js."
fi

NODE_VER="${FLUME_NODEJS_LTS_VERSION:-v20.18.1}"
UNAME_S="$(uname -s)"
ARCH_RAW="$(uname -m)"

case "${ARCH_RAW}" in
    x86_64) NODE_ARCH="x64" ;;
    aarch64|arm64) NODE_ARCH="arm64" ;;
    *) error "Unsupported CPU architecture: ${ARCH_RAW} (need x86_64 or arm64)" ;;
esac

case "${UNAME_S}" in
    Linux)
        OS="linux"
        EXT="tar.xz"
        ;;
    Darwin)
        OS="darwin"
        EXT="tar.gz"
        ;;
    *)
        error "Unsupported OS: ${UNAME_S} (supported: Linux, Darwin)"
        ;;
esac

NAME="node-${NODE_VER}-${OS}-${NODE_ARCH}"
URL="https://nodejs.org/dist/${NODE_VER}/${NAME}.${EXT}"
TMP_DIR="$(mktemp -d)"

info "Downloading Node.js ${NODE_VER} for ${OS}-${NODE_ARCH}..."
curl -fsSL "${URL}" -o "${TMP_DIR}/node.${EXT}"

info "Installing into /usr/local ..."
if [ "${EXT}" = "tar.xz" ]; then
    tar -xJf "${TMP_DIR}/node.${EXT}" -C "${TMP_DIR}"
else
    tar -xzf "${TMP_DIR}/node.${EXT}" -C "${TMP_DIR}"
fi
# Archive contains a single top-level directory ${NAME}/
if [ ! -d "${TMP_DIR}/${NAME}" ]; then
    rm -rf "${TMP_DIR}"
    error "Unexpected Node.js archive layout (missing ${NAME}/)."
fi
cp -R "${TMP_DIR}/${NAME}/." /usr/local/
rm -rf "${TMP_DIR}"

hash -r 2>/dev/null || true
if ! command -v node >/dev/null 2>&1; then
    error "node not on PATH after install (expected /usr/local/bin/node)."
fi
if ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' 2>/dev/null; then
    error "Installed Node.js is older than 18 — check FLUME_NODEJS_LTS_VERSION."
fi

success "Node.js $(node --version), npm $(npm --version 2>/dev/null || echo '?') installed."
