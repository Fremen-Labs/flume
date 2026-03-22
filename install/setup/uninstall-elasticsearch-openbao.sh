#!/usr/bin/env bash
# Flume — remove native Elasticsearch + OpenBao CLI (clean slate for “new user” testing)
#
# Stops the ES service, removes the distro package, drops Elastic repo config,
# deletes residual data/log directories, and removes the OpenBao CLI binary.
#
# Usage:
#   sudo bash install/setup/uninstall-elasticsearch-openbao.sh
#
# Docker: this script does not remove images. As a normal user:
#   docker images | grep -i elastic
#   docker rmi <image_id>   # optional

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
    error "Run as root: sudo $0"
fi

detect_pkg_manager() {
    if command -v dnf &>/dev/null; then echo "dnf"
    elif command -v yum &>/dev/null; then echo "yum"
    elif command -v apt-get &>/dev/null; then echo "apt"
    else echo "none"
    fi
}

PKG_MGR="$(detect_pkg_manager)"
info "Package manager: ${PKG_MGR}"

# --- Elasticsearch (systemd + package) ---
if systemctl list-unit-files elasticsearch.service &>/dev/null 2>&1; then
    if systemctl is-enabled elasticsearch.service &>/dev/null 2>&1; then
        info "Disabling elasticsearch.service..."
        systemctl disable elasticsearch --now 2>/dev/null || systemctl disable elasticsearch 2>/dev/null || true
    fi
    if systemctl is-active elasticsearch.service &>/dev/null 2>&1; then
        info "Stopping elasticsearch.service..."
        systemctl stop elasticsearch || true
    fi
fi

if rpm -q elasticsearch &>/dev/null 2>&1; then
    info "Removing elasticsearch RPM..."
    if command -v dnf &>/dev/null; then
        dnf remove -y elasticsearch
    else
        yum remove -y elasticsearch
    fi
    success "Elasticsearch package removed."
elif command -v dpkg-query &>/dev/null && dpkg-query -W -f='${Status}' elasticsearch 2>/dev/null | grep -q 'install ok installed'; then
    info "Removing elasticsearch package (apt)..."
    DEBIAN_FRONTEND=noninteractive apt-get remove -y --purge elasticsearch || true
    apt-get autoremove -y || true
    success "Elasticsearch package removed."
else
    warn "No elasticsearch package found via rpm/dpkg (may already be uninstalled)."
fi

# Residual paths (data survives package removal on many installs)
for d in /var/lib/elasticsearch /var/log/elasticsearch; do
    if [ -d "$d" ]; then
        info "Removing $d ..."
        rm -rf "$d"
    fi
done

# Elastic repo / apt source (so the machine looks like before first install)
if [ -f /etc/yum.repos.d/elasticsearch.repo ]; then
    rm -f /etc/yum.repos.d/elasticsearch.repo
    success "Removed /etc/yum.repos.d/elasticsearch.repo"
fi
if [ -f /etc/apt/sources.list.d/elastic-8.x.list ]; then
    rm -f /etc/apt/sources.list.d/elastic-8.x.list
    success "Removed elastic APT source list"
fi
if [ -f /usr/share/keyrings/elasticsearch-keyring.gpg ]; then
    rm -f /usr/share/keyrings/elasticsearch-keyring.gpg
    info "Removed elasticsearch APT keyring"
fi

# --- OpenBao CLI (Flume install-openbao.sh drops binary here) ---
if [ -e /usr/local/bin/openbao ]; then
    info "Removing /usr/local/bin/openbao ..."
    rm -f /usr/local/bin/openbao
    success "OpenBao CLI removed."
else
    warn "No /usr/local/bin/openbao (already absent)."
fi
# Upstream name; only remove if it is our large static binary (avoid clobbering unrelated tools named bao)
if [ -f /usr/local/bin/bao ] && [ "$(stat -c%s /usr/local/bin/bao 2>/dev/null || echo 0)" -gt 50000000 ]; then
    info "Removing /usr/local/bin/bao (OpenBao-sized binary) ..."
    rm -f /usr/local/bin/bao
    success "bao binary removed."
fi

systemctl daemon-reload 2>/dev/null || true

echo ""
success "Elasticsearch + OpenBao CLI uninstall finished."
info "Optional: remove unused Docker images — docker images | grep -i elastic"
