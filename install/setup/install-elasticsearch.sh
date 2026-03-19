#!/usr/bin/env bash
# Flume — Native Elasticsearch 8 installer
#
# Supports:
#   - RHEL / CentOS / Rocky Linux / AlmaLinux (dnf/yum)
#   - Debian / Ubuntu (apt)
#
# What this script does:
#   1. Detects the OS package manager
#   2. Adds the official Elastic 8 repository
#   3. Installs elasticsearch
#   4. Configures elasticsearch.yml for single-node with TLS
#   5. Enables and starts the elasticsearch systemd service
#   6. Resets the built-in 'elastic' password
#   7. Generates a Flume API key
#   8. Writes bootstrap credentials to .es-bootstrap.env in the workspace
#
# install.sh consumes .es-bootstrap.env to configure ES automatically.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BOOTSTRAP_ENV_FILE="${WORKSPACE_ROOT}/.es-bootstrap.env"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# Must run as root or with sudo
if [ "$EUID" -ne 0 ]; then
    error "This script must be run as root (use: sudo $0)"
fi

echo ""
echo "========================================"
echo "  Flume — Elasticsearch 8 Installer"
echo "========================================"
echo ""

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_ID_LIKE="${ID_LIKE:-}"
else
    error "Cannot detect OS. /etc/os-release not found."
fi

detect_pkg_manager() {
    if command -v dnf &>/dev/null; then echo "dnf"
    elif command -v yum &>/dev/null; then echo "yum"
    elif command -v apt-get &>/dev/null; then echo "apt"
    else error "No supported package manager found (dnf, yum, or apt-get)."
    fi
}

PKG_MGR=$(detect_pkg_manager)
info "Detected package manager: ${PKG_MGR}"
info "OS: ${PRETTY_NAME:-${OS_ID}}"
echo ""

# ----------------------------------------------------------------
# Install Elasticsearch
# ----------------------------------------------------------------

if command -v elasticsearch &>/dev/null || systemctl list-unit-files elasticsearch.service &>/dev/null 2>&1; then
    warn "Elasticsearch appears to already be installed. Skipping package install."
else
    if [ "$PKG_MGR" = "apt" ]; then
        info "Adding Elastic APT repository..."
        apt-get install -y wget gnupg apt-transport-https
        wget -qO - https://artifacts.elastic.co/GPG-KEY-elasticsearch \
            | gpg --dearmor > /usr/share/keyrings/elasticsearch-keyring.gpg
        echo "deb [signed-by=/usr/share/keyrings/elasticsearch-keyring.gpg] \
https://artifacts.elastic.co/packages/8.x/apt stable main" \
            > /etc/apt/sources.list.d/elastic-8.x.list
        apt-get update -y
        info "Installing Elasticsearch..."
        apt-get install -y elasticsearch
    else
        info "Adding Elastic RPM repository..."
        rpm --import https://artifacts.elastic.co/GPG-KEY-elasticsearch
        cat > /etc/yum.repos.d/elasticsearch.repo << 'REPO'
[elasticsearch]
name=Elasticsearch repository for 8.x packages
baseurl=https://artifacts.elastic.co/packages/8.x/yum
gpgcheck=1
gpgkey=https://artifacts.elastic.co/GPG-KEY-elasticsearch
enabled=1
autorefresh=1
type=rpm-md
REPO
        info "Installing Elasticsearch..."
        $PKG_MGR install -y elasticsearch
    fi
    success "Elasticsearch installed."
fi

# ----------------------------------------------------------------
# Configure elasticsearch.yml
# ----------------------------------------------------------------
ES_CONF="/etc/elasticsearch/elasticsearch.yml"
info "Configuring ${ES_CONF}..."

# Back up original config
cp "${ES_CONF}" "${ES_CONF}.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true

cat > "${ES_CONF}" << 'EOF'
# Flume — Elasticsearch single-node configuration
cluster.name: flume
node.name: flume-node-1

# Store data and logs under default paths
path.data: /var/lib/elasticsearch
path.logs: /var/log/elasticsearch

# Listen on all interfaces (change to 127.0.0.1 for localhost-only)
network.host: 0.0.0.0

# HTTP port
http.port: 9200

# Single-node discovery
discovery.type: single-node

# Security enabled with TLS (Elasticsearch 8 default)
xpack.security.enabled: true
xpack.security.http.ssl:
  enabled: true
  keystore.path: certs/http.p12
xpack.security.transport.ssl:
  enabled: true
  verification_mode: certificate
  keystore.path: certs/transport.p12
  truststore.path: certs/transport.p12
EOF

success "elasticsearch.yml configured."

# ----------------------------------------------------------------
# Enable and start service
# ----------------------------------------------------------------
info "Enabling and starting Elasticsearch service..."
systemctl daemon-reload
systemctl enable elasticsearch
systemctl start elasticsearch

# Wait for ES to be ready
info "Waiting for Elasticsearch to start (up to 60 seconds)..."
for i in $(seq 1 12); do
    if curl -sk "https://localhost:9200/" &>/dev/null; then
        break
    fi
    sleep 5
    echo -n "."
done
echo ""

if ! curl -sk "https://localhost:9200/" &>/dev/null; then
    warn "Elasticsearch may not be fully started yet. Check: journalctl -u elasticsearch -f"
fi

success "Elasticsearch service is running."

# ----------------------------------------------------------------
# Reset elastic password (batch mode: auto-generates, no old password needed)
# ----------------------------------------------------------------
info "Resetting the 'elastic' superuser password..."
RESET_OUTPUT=$(/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1) || true
# Parse "New value: <password>" from output (batch mode prints the new password)
ES_PASSWORD=$(echo "$RESET_OUTPUT" | grep "New value:" | sed 's/.*New value:[[:space:]]*//' | tr -d '\n\r' | head -c 256)
if [ -z "$ES_PASSWORD" ]; then
    warn "Batch reset did not return password. Trying interactive mode with random password..."
    ES_PASSWORD=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24)
    echo -e "${ES_PASSWORD}\n${ES_PASSWORD}" | /usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -i --force 2>/dev/null || true
fi
success "elastic password set."

# ----------------------------------------------------------------
# Generate Flume API key
# ----------------------------------------------------------------
info "Generating Flume API key..."
sleep 3

API_KEY_RESPONSE=$(curl -sk -u "elastic:${ES_PASSWORD}" \
    -X POST "https://localhost:9200/_security/api_key" \
    -H "Content-Type: application/json" \
    -d '{"name":"flume","role_descriptors":{}}')

API_KEY_ID=$(echo "$API_KEY_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['id'])" 2>/dev/null || echo "")
API_KEY_SECRET=$(echo "$API_KEY_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['api_key'])" 2>/dev/null || echo "")
API_KEY_ENCODED=$(echo "$API_KEY_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['encoded'])" 2>/dev/null || echo "")

if [ -z "$API_KEY_ENCODED" ]; then
    warn "Could not automatically generate API key. Generate one manually:"
    echo ""
    echo "  curl -sk -u 'elastic:${ES_PASSWORD}' \\"
    echo "    -X POST 'https://localhost:9200/_security/api_key' \\"
    echo "    -H 'Content-Type: application/json' \\"
    echo "    -d '{\"name\":\"flume\",\"role_descriptors\":{}}'"
    echo ""
else
    # Persist bootstrap credentials for install.sh to consume automatically.
    {
        echo "ES_URL=https://localhost:9200"
        echo "ES_API_KEY=${API_KEY_ENCODED}"
        echo "ES_VERIFY_TLS=false"
        echo "ES_BOOTSTRAP_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "${BOOTSTRAP_ENV_FILE}"
    chmod 600 "${BOOTSTRAP_ENV_FILE}" 2>/dev/null || true
    # Ensure the user who ran sudo can read it (install.sh runs as that user)
    if [ -n "${SUDO_UID:-}" ] && [ -n "${SUDO_GID:-}" ]; then
        chown "${SUDO_UID}:${SUDO_GID}" "${BOOTSTRAP_ENV_FILE}" 2>/dev/null || true
    fi
    chmod 644 "${BOOTSTRAP_ENV_FILE}" 2>/dev/null || true

    echo ""
    echo "========================================"
    echo -e "${GREEN}  Installation complete!${NC}"
    echo "========================================"
    echo ""
    echo "Elasticsearch credentials were written to:"
    echo -e "  ${YELLOW}${BOOTSTRAP_ENV_FILE}${NC}"
    echo ""
    echo "install.sh will auto-apply these values to .env:"
    echo ""
    echo -e "  ${YELLOW}ES_URL=https://localhost:9200${NC}"
    echo -e "  ${YELLOW}ES_API_KEY=${API_KEY_ENCODED}${NC}"
    echo -e "  ${YELLOW}ES_VERIFY_TLS=false${NC}"
    echo ""
    echo "elastic superuser password: ${ES_PASSWORD}"
    echo "(save this somewhere safe — you may need it to regenerate API keys)"
    echo ""
fi
