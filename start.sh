#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# SpiderPanel Universal Installer / Manager
# Supports:
#   - Ubuntu / Debian
#   - Arch / Omarchy
#   - Fedora / RHEL / CentOS
#   - VPS with systemd
#   - GitHub Codespaces without systemd
#
# Fixed application port: 8080
# Python runtime: 3.12
# ============================================================

APP_NAME="SpiderPanel"

APP_DIR="${SPIDER_APP_DIR:-/opt/SpiderPanel}"
ENV_FILE="/etc/spider-panel.env"

REPO="${SPIDER_REPO:-https://github.com/amirh00sain/SpiderPanel.git}"
BRANCH="${SPIDER_BRANCH:-main}"

INSTALLER_URL="${SPIDER_INSTALLER_URL:-https://raw.githubusercontent.com/amirh00sain/SpiderPanel/main/start.sh}"

SERVICE_NAME="spider-panel"

PORT="8080"

VENV_DIR="${APP_DIR}/.venv"
PID_FILE="${APP_DIR}/spiderpanel.pid"
LOG_FILE="${APP_DIR}/spiderpanel.log"

XRAY_VERSION="26.3.27"
XRAY_DIR="${APP_DIR}/xray"
XRAY_BIN="${XRAY_DIR}/xray"

MTPROXY_BIN="/usr/local/bin/mtproto-proxy"

CLI_PATH="/usr/local/bin/spiderpanel"

TMP_ROOT=""

# ------------------------------------------------------------
# Colors
# ------------------------------------------------------------

if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    CYAN='\033[0;36m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    CYAN=''
    NC=''
fi

log() {
    echo -e "${BLUE}[SpiderPanel]${NC} $*"
}

ok() {
    echo -e "${GREEN}[OK]${NC} $*"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

fail() {
    error "$*"
    exit 1
}

# ------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------

cleanup() {
    if [[ -n "${TMP_ROOT:-}" && -d "${TMP_ROOT:-}" ]]; then
        rm -rf "$TMP_ROOT" || true
    fi
}

trap cleanup EXIT

# ------------------------------------------------------------
# Root escalation
# ------------------------------------------------------------

ensure_root() {
    if [[ "${EUID}" -eq 0 ]]; then
        return 0
    fi

    log "Root privileges required."
    log "Re-launching installer with sudo..."

    local tmp_installer
    tmp_installer="$(mktemp /tmp/spiderpanel-start.XXXXXX.sh)"

    if ! curl -fsSL "$INSTALLER_URL" -o "$tmp_installer"; then
        rm -f "$tmp_installer"
        fail "Could not download installer for sudo execution."
    fi

    chmod 700 "$tmp_installer"

    exec sudo -E env \
        HOME=/root \
        DEBIAN_FRONTEND=noninteractive \
        bash "$tmp_installer" "$@"
}

# ------------------------------------------------------------
# OS detection
# ------------------------------------------------------------

OS_ID=""
OS_NAME=""
OS_VERSION=""

detect_os() {
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        source /etc/os-release
        OS_ID="${ID:-unknown}"
        OS_NAME="${NAME:-unknown}"
        OS_VERSION="${VERSION_ID:-unknown}"
    else
        OS_ID="unknown"
        OS_NAME="unknown"
        OS_VERSION="unknown"
    fi
}

# ------------------------------------------------------------
# Environment detection
# ------------------------------------------------------------

IS_CODESPACE="false"
HAS_SYSTEMD="false"

detect_environment() {
    if [[ "${CODESPACES:-false}" == "true" ]] || [[ -n "${CODESPACE_NAME:-}" ]]; then
        IS_CODESPACE="true"
    fi

    if command -v systemctl >/dev/null 2>&1; then
        if [[ -d /run/systemd/system ]] || [[ "$(ps -p 1 -o comm= 2>/dev/null || true)" == "systemd" ]]; then
            HAS_SYSTEMD="true"
        fi
    fi
}

# ------------------------------------------------------------
# Basic dependencies
# ------------------------------------------------------------

install_packages_apt() {
    log "Installing system packages with apt..."

    apt-get update -y

    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        ca-certificates \
        curl \
        git \
        unzip \
        xz-utils \
        tar \
        gzip \
        rsync \
        python3 \
        python3-venv \
        python3-pip \
        build-essential \
        gcc \
        g++ \
        make \
        pkg-config \
        openssl \
        libssl-dev \
        zlib1g-dev \
        procps \
        iproute2 \
        iputils-ping \
        net-tools \
        lsof \
        jq \
        gnupg \
        lsb-release

    ok "apt dependencies installed."
}

install_packages_dnf() {
    log "Installing system packages with dnf..."

    dnf -y install \
        ca-certificates \
        curl \
        git \
        unzip \
        xz \
        tar \
        gzip \
        rsync \
        python3 \
        python3-pip \
        gcc \
        gcc-c++ \
        make \
        pkgconf-pkg-config \
        openssl \
        openssl-devel \
        zlib-devel \
        procps-ng \
        iproute \
        iputils \
        net-tools \
        lsof \
        jq \
        gnupg2

    ok "dnf dependencies installed."
}

install_packages_pacman() {
    log "Installing system packages with pacman..."

    pacman -Sy --noconfirm \
        ca-certificates \
        curl \
        git \
        unzip \
        xz \
        tar \
        gzip \
        rsync \
        python \
        python-pip \
        base-devel \
        openssl \
        zlib \
        procps-ng \
        iproute2 \
        iputils \
        net-tools \
        lsof \
        jq

    ok "pacman dependencies installed."
}

install_base_packages() {
    case "$OS_ID" in
        ubuntu|debian|linuxmint|pop)
            install_packages_apt
            ;;
        fedora|rhel|centos|rocky|almalinux)
            install_packages_dnf
            ;;
        arch|manjaro|endeavouros)
            install_packages_pacman
            ;;
        *)
            warn "Unknown distribution: $OS_ID"
            warn "Attempting to continue..."
            ;;
    esac
}

# ------------------------------------------------------------
# Docker
# ------------------------------------------------------------

install_docker() {
    if command -v docker >/dev/null 2>&1; then
        ok "Docker already installed."
        return 0
    fi

    if [[ "$IS_CODESPACE" == "true" ]]; then
        log "GitHub Codespaces detected. Skipping Docker installation."
        return 0
    fi

    log "Installing Docker..."

    if command -v apt-get >/dev/null 2>&1; then

        curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
        sh /tmp/get-docker.sh
        rm -f /tmp/get-docker.sh

    elif command -v dnf >/dev/null 2>&1; then

        dnf -y install dnf-plugins-core
        dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo || true

        dnf -y install \
            docker-ce \
            docker-ce-cli \
            containerd.io \
            docker-buildx-plugin \
            docker-compose-plugin || true

    elif command -v pacman >/dev/null 2>&1; then

        pacman -S --noconfirm docker || true

    else
        warn "Could not install Docker automatically."
        return 0
    fi

    if command -v systemctl >/dev/null 2>&1 && [[ "$HAS_SYSTEMD" == "true" ]]; then
        systemctl enable --now docker >/dev/null 2>&1 || true
    fi

    if command -v docker >/dev/null 2>&1; then
        ok "Docker installed."
    else
        warn "Docker installation was not completed."
    fi
}

# ------------------------------------------------------------
# Repository
# ------------------------------------------------------------

download_repository() {
    TMP_ROOT="$(mktemp -d /tmp/spiderpanel.XXXXXX)"

    log "Downloading SpiderPanel..."

    git clone \
        --depth 1 \
        --branch "$BRANCH" \
        --single-branch \
        "$REPO" \
        "$TMP_ROOT/app"

    ok "Repository downloaded."
}

# ------------------------------------------------------------
# Deploy files
# ------------------------------------------------------------

deploy_application() {
    mkdir -p "$APP_DIR"

    # Preserve important local files.
    if [[ -d "$APP_DIR/data" ]]; then
        mkdir -p "$TMP_ROOT/old-data"
        cp -a "$APP_DIR/data" "$TMP_ROOT/old-data/" || true
    fi

    if [[ -f "$APP_DIR/.env" ]]; then
        cp "$APP_DIR/.env" "$TMP_ROOT/old.env" || true
    fi

    if [[ -f "$ENV_FILE" ]]; then
        cp "$ENV_FILE" "$TMP_ROOT/old-spider-panel.env" || true
    fi

    log "Installing application files..."

    rsync -a \
        --delete \
        --exclude '.git/' \
        --exclude '.venv/' \
        --exclude 'data/' \
        --exclude '*.log' \
        --exclude '*.pid' \
        "$TMP_ROOT/app/" \
        "$APP_DIR/"

    # Restore preserved data.
    if [[ -d "$TMP_ROOT/old-data/data" ]]; then
        mkdir -p "$APP_DIR/data"
        rsync -a "$TMP_ROOT/old-data/data/" "$APP_DIR/data/" || true
    fi

    # Restore local .env if it existed.
    if [[ -f "$TMP_ROOT/old.env" ]]; then
        cp "$TMP_ROOT/old.env" "$APP_DIR/.env"
    fi

    ok "Application installed."
}

# ------------------------------------------------------------
# Install uv correctly
# ------------------------------------------------------------

install_uv() {

    log "Preparing dedicated Python 3.12 runtime..."

    local UV_PATH="/usr/local/bin/uv"
    local UV_INSTALLER

    # Already working?
    if [[ -x "$UV_PATH" ]]; then
        if "$UV_PATH" --version >/dev/null 2>&1; then
            ok "uv already installed: $("$UV_PATH" --version)"
            return 0
        fi
    fi

    # Remove potentially broken files.
    rm -f \
        /usr/local/bin/uv \
        /usr/local/bin/uvx \
        /usr/local/uv \
        /usr/local/uvx \
        2>/dev/null || true

    log "Installing uv..."

    UV_INSTALLER="$(mktemp /tmp/spiderpanel-uv.XXXXXX.sh)"

    if ! curl -fsSL https://astral.sh/uv/install.sh -o "$UV_INSTALLER"; then
        rm -f "$UV_INSTALLER"
        fail "Could not download uv installer."
    fi

    chmod 700 "$UV_INSTALLER"

    # IMPORTANT:
    # UV_UNMANAGED_INSTALL=/usr/local/bin
    # makes the installer place uv exactly here.
    if ! UV_UNMANAGED_INSTALL="/usr/local/bin" \
        bash "$UV_INSTALLER"; then

        rm -f "$UV_INSTALLER"
        fail "uv installer failed."
    fi

    rm -f "$UV_INSTALLER"

    hash -r 2>/dev/null || true

    if [[ ! -x "$UV_PATH" ]]; then
        fail "uv installation failed: $UV_PATH was not created."
    fi

    if ! "$UV_PATH" --version >/dev/null 2>&1; then
        fail "uv was installed but cannot execute."
    fi

    ok "uv installed: $("$UV_PATH" --version)"
}

# ------------------------------------------------------------
# Python 3.12
# ------------------------------------------------------------

setup_python() {

    install_uv

    local UV="/usr/local/bin/uv"
    local PY312=""
    local EXISTING_PYTHON=""
    local EXISTING_VERSION=""
    local PYTHON_BIN=""

    log "Installing Python 3.12 with uv..."

    "$UV" python install 3.12

    PY312="$("$UV" python find 3.12 2>/dev/null | head -n 1 || true)"

    if [[ -z "$PY312" ]]; then
        fail "uv could not find Python 3.12."
    fi

    if [[ ! -x "$PY312" ]]; then
        fail "Python 3.12 binary is not executable: $PY312"
    fi

    local FOUND_VERSION
    FOUND_VERSION="$("$PY312" -c 'import sys; print(".".join(map(str,sys.version_info[:3])))')"

    if [[ "$FOUND_VERSION" != 3.12.* ]]; then
        fail "uv returned wrong Python version: $FOUND_VERSION"
    fi

    ok "Python selected: $PY312 ($FOUND_VERSION)"

    # --------------------------------------------------------
    # Remove old Python 3.14 environment.
    # --------------------------------------------------------

    EXISTING_PYTHON="$VENV_DIR/bin/python"

    if [[ -x "$EXISTING_PYTHON" ]]; then

        EXISTING_VERSION="$(
            "$EXISTING_PYTHON" -c \
                'import sys; print(".".join(map(str,sys.version_info[:2])))' \
                2>/dev/null || true
        )"

        if [[ "$EXISTING_VERSION" != "3.12" ]]; then
            warn "Existing .venv uses Python $EXISTING_VERSION."
            log "Removing old virtual environment..."
            rm -rf "$VENV_DIR"
        fi
    fi

    # --------------------------------------------------------
    # Create fresh venv.
    # --------------------------------------------------------

    if [[ ! -x "$VENV_DIR/bin/python" ]]; then

        log "Creating Python 3.12 virtual environment..."

        "$UV" venv \
            --python "$PY312" \
            "$VENV_DIR"
    fi

    PYTHON_BIN="$VENV_DIR/bin/python"

    if [[ ! -x "$PYTHON_BIN" ]]; then
        fail "Virtual environment Python was not created."
    fi

    local VENV_VERSION
    VENV_VERSION="$(
        "$PYTHON_BIN" -c \
            'import sys; print(".".join(map(str,sys.version_info[:3])))'
    )"

    if [[ "$VENV_VERSION" != 3.12.* ]]; then
        fail "Virtual environment is not Python 3.12: $VENV_VERSION"
    fi

    ok "Virtual environment ready: Python $VENV_VERSION"

    # --------------------------------------------------------
    # Upgrade packaging tools.
    # --------------------------------------------------------

    log "Upgrading pip / setuptools / wheel..."

    "$PYTHON_BIN" -m pip install \
        --upgrade \
        --no-cache-dir \
        pip \
        setuptools \
        wheel

    # --------------------------------------------------------
    # Install application dependencies.
    # --------------------------------------------------------

    if [[ ! -f "$APP_DIR/requirements.txt" ]]; then
        fail "requirements.txt was not found."
    fi

    log "Installing SpiderPanel Python dependencies..."

    "$PYTHON_BIN" -m pip install \
        --no-cache-dir \
        -r "$APP_DIR/requirements.txt"

    # --------------------------------------------------------
    # Import test.
    # --------------------------------------------------------

    log "Testing Python dependencies..."

    "$PYTHON_BIN" - <<'PY'
import fastapi
import uvicorn
import httpx
import websockets
import aiofiles
import qrcode
import PIL
import psutil
import cryptography
import socks

print("FastAPI:", fastapi.__version__)
print("Uvicorn:", uvicorn.__version__)
print("HTTPX:", httpx.__version__)
print("Pillow:", PIL.__version__)
print("Python: 3.12")
print("Dependency test: OK")
PY

    ok "Python dependencies installed successfully."
}

# ------------------------------------------------------------
# Xray
# ------------------------------------------------------------

install_xray() {

    mkdir -p "$XRAY_DIR"

    if [[ -x "$XRAY_BIN" ]]; then
        if "$XRAY_BIN" version >/dev/null 2>&1; then
            ok "Xray already installed."
            return 0
        fi
    fi

    log "Installing Xray $XRAY_VERSION..."

    local ARCH
    local XRAY_ARCH
    local URL
    local ZIP

    ARCH="$(uname -m)"

    case "$ARCH" in
        x86_64)
            XRAY_ARCH="64"
            ;;
        aarch64|arm64)
            XRAY_ARCH="arm64-v8a"
            ;;
        armv7l)
            XRAY_ARCH="arm32-v7a"
            ;;
        *)
            warn "Unsupported Xray architecture: $ARCH"
            return 0
            ;;
    esac

    ZIP="$APP_DIR/xray.zip"

    URL="https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-linux-${XRAY_ARCH}.zip"

    if ! curl -fL \
        --retry 3 \
        --retry-delay 2 \
        "$URL" \
        -o "$ZIP"; then

        rm -f "$ZIP"
        warn "Xray download failed."
        return 0
    fi

    rm -rf "$XRAY_DIR"
    mkdir -p "$XRAY_DIR"

    if ! unzip -o "$ZIP" -d "$XRAY_DIR" >/dev/null; then
        rm -f "$ZIP"
        warn "Xray archive extraction failed."
        return 0
    fi

    rm -f "$ZIP"

    if [[ -f "$XRAY_BIN" ]]; then
        chmod +x "$XRAY_BIN"

        if "$XRAY_BIN" version >/dev/null 2>&1; then
            ok "Xray installed."
        else
            warn "Xray binary exists but failed version test."
        fi
    else
        warn "Xray binary not found after extraction."
    fi
}

# ------------------------------------------------------------
# MTProxy
# ------------------------------------------------------------

install_mtproxy() {

    if [[ -x "$MTPROXY_BIN" ]]; then
        ok "MTProxy already installed."
        return 0
    fi

    if ! command -v git >/dev/null 2>&1; then
        warn "git is required for MTProxy."
        return 0
    fi

    local MT_TMP
    MT_TMP="$(mktemp -d /tmp/mtproxy.XXXXXX)"

    log "Building Telegram MTProxy..."

    if ! git clone \
        --depth 1 \
        https://github.com/TelegramMessenger/MTProxy.git \
        "$MT_TMP/MTProxy"; then

        rm -rf "$MT_TMP"
        warn "MTProxy repository download failed."
        return 0
    fi

    if ! make -C "$MT_TMP/MTProxy" -j"$(nproc 2>/dev/null || echo 2)"; then
        rm -rf "$MT_TMP"
        warn "MTProxy compilation failed."
        return 0
    fi

    local MT_BIN=""

    if [[ -x "$MT_TMP/MTProxy/objs/bin/mtproto-proxy" ]]; then
        MT_BIN="$MT_TMP/MTProxy/objs/bin/mtproto-proxy"
    elif [[ -x "$MT_TMP/MTProxy/mtproto-proxy" ]]; then
        MT_BIN="$MT_TMP/MTProxy/mtproto-proxy"
    fi

    if [[ -z "$MT_BIN" ]]; then
        rm -rf "$MT_TMP"
        warn "MTProxy binary not found."
        return 0
    fi

    install -m 0755 "$MT_BIN" "$MTPROXY_BIN"

    rm -rf "$MT_TMP"

    if [[ -x "$MTPROXY_BIN" ]]; then
        ok "MTProxy installed."
    else
        warn "MTProxy installation failed."
    fi
}

# ------------------------------------------------------------
# Environment file
# ------------------------------------------------------------

random_hex() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
        return 0
    fi

    if [[ -r /dev/urandom ]]; then
        od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
        return 0
    fi

    date +%s%N
}

generate_password() {
    local p
    p="$(random_hex | head -c 20)"
    echo "$p"
}

setup_environment() {

    log "Configuring SpiderPanel environment..."

    local SECRET_KEY=""
    local ADMIN_PASSWORD=""
    local EXISTING_SECRET=""
    local EXISTING_PASSWORD=""

    if [[ -f "$ENV_FILE" ]]; then

        EXISTING_SECRET="$(
            grep '^SECRET_KEY=' "$ENV_FILE" \
            | head -n1 \
            | cut -d= -f2- \
            || true
        )"

        EXISTING_PASSWORD="$(
            grep '^ADMIN_PASSWORD=' "$ENV_FILE" \
            | head -n1 \
            | cut -d= -f2- \
            || true
        )
    fi

    SECRET_KEY="${EXISTING_SECRET:-$(random_hex)}"
    ADMIN_PASSWORD="${EXISTING_PASSWORD:-$(generate_password)}"

    mkdir -p "$(dirname "$ENV_FILE")"

    cat > "$ENV_FILE" <<EOF
SECRET_KEY=$SECRET_KEY
ADMIN_PASSWORD=$ADMIN_PASSWORD

PORT=$PORT

DATA_DIR=$APP_DIR/data
SPIDER_DATA_DIR=$APP_DIR/data

XRAY_BIN=$XRAY_BIN
MTPROTO_PROXY_BIN=$MTPROXY_BIN

WORKER_SYNC_INTERVAL=3600

RAILWAY_PUBLIC_DOMAIN=

PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
PIP_NO_CACHE_DIR=1
EOF

    chmod 600 "$ENV_FILE"

    mkdir -p "$APP_DIR/data"

    # Human-readable credentials file.
    cat > "$APP_DIR/INSTALL-CREDENTIALS.txt" <<EOF
========================================
 SpiderPanel
========================================

Application:
  $APP_NAME

Port:
  $PORT

Local URL:
  http://127.0.0.1:$PORT/spider

Admin Password:
  $ADMIN_PASSWORD

Environment file:
  $ENV_FILE

Application directory:
  $APP_DIR

Python:
  $VENV_DIR/bin/python

Xray:
  $XRAY_BIN

MTProxy:
  $MTPROXY_BIN

========================================
EOF

    chmod 600 "$APP_DIR/INSTALL-CREDENTIALS.txt"

    ok "Environment configured."
}

# ------------------------------------------------------------
# Application validation
# ------------------------------------------------------------

validate_application() {

    log "Validating SpiderPanel..."

    local PYTHON="$VENV_DIR/bin/python"

    if [[ ! -x "$PYTHON" ]]; then
        fail "Python virtual environment is missing."
    fi

    if [[ ! -f "$APP_DIR/main.py" ]]; then
        warn "main.py not found in repository root."
    else
        "$PYTHON" -m py_compile "$APP_DIR/main.py"
    fi

    "$PYTHON" -m compileall -q "$APP_DIR"

    ok "Application validation completed."
}

# ------------------------------------------------------------
# Systemd
# ------------------------------------------------------------

create_systemd_service() {

    if [[ "$HAS_SYSTEMD" != "true" ]]; then
        return 0
    fi

    log "Creating systemd service..."

    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=SpiderPanel Control Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root

WorkingDirectory=$APP_DIR

EnvironmentFile=$ENV_FILE

Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PIP_NO_CACHE_DIR=1

ExecStart=$VENV_DIR/bin/uvicorn main:app --host 0.0.0.0 --port $PORT

Restart=always
RestartSec=5

TimeoutStartSec=180
TimeoutStopSec=30

KillMode=mixed

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true

    ok "Systemd service created."
}

# ------------------------------------------------------------
# Standalone process
# ------------------------------------------------------------

is_running_standalone() {

    if [[ ! -f "$PID_FILE" ]]; then
        return 1
    fi

    local PID
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"

    [[ -n "$PID" ]] || return 1

    kill -0 "$PID" >/dev/null 2>&1
}

start_standalone() {

    local PYTHON="$VENV_DIR/bin/uvicorn"

    if [[ ! -x "$PYTHON" ]]; then
        fail "Uvicorn was not installed."
    fi

    if is_running_standalone; then
        ok "SpiderPanel is already running."
        return 0
    fi

    log "Starting SpiderPanel in standalone mode..."

    cd "$APP_DIR"

    nohup "$PYTHON" \
        main:app \
        --host 0.0.0.0 \
        --port "$PORT" \
        >> "$LOG_FILE" 2>&1 &

    echo $! > "$PID_FILE"

    sleep 3

    if is_running_standalone; then
        ok "SpiderPanel started."
    else
        error "SpiderPanel failed to start."
        tail -n 80 "$LOG_FILE" 2>/dev/null || true
        return 1
    fi
}

stop_standalone() {

    if ! is_running_standalone; then
        rm -f "$PID_FILE"
        return 0
    fi

    local PID
    PID="$(cat "$PID_FILE")"

    log "Stopping SpiderPanel..."

    kill "$PID" >/dev/null 2>&1 || true

    for _ in {1..20}; do
        if ! kill -0 "$PID" >/dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done

    if kill -0 "$PID" >/dev/null 2>&1; then
        kill -9 "$PID" >/dev/null 2>&1 || true
    fi

    rm -f "$PID_FILE"

    ok "SpiderPanel stopped."
}

# ------------------------------------------------------------
# Start
# ------------------------------------------------------------

start_panel() {

    if [[ "$HAS_SYSTEMD" == "true" ]]; then

        create_systemd_service

        systemctl restart "$SERVICE_NAME"

        sleep 4

        if systemctl is-active --quiet "$SERVICE_NAME"; then
            ok "SpiderPanel is running via systemd."
        else
            error "SpiderPanel systemd service failed."
            journalctl -u "$SERVICE_NAME" -n 80 --no-pager || true
            return 1
        fi

    else

        start_standalone
    fi
}

# ------------------------------------------------------------
# Stop
# ------------------------------------------------------------

stop_panel() {

    if [[ "$HAS_SYSTEMD" == "true" ]]; then

        if systemctl is-active --quiet "$SERVICE_NAME"; then
            systemctl stop "$SERVICE_NAME"
            ok "SpiderPanel stopped."
        else
            warn "SpiderPanel is not running."
        fi

    else

        stop_standalone
    fi
}

# ------------------------------------------------------------
# Restart
# ------------------------------------------------------------

restart_panel() {

    stop_panel || true
    sleep 1
    start_panel
}

# ------------------------------------------------------------
# Status
# ------------------------------------------------------------

status_panel() {

    echo
    echo "========================================"
    echo " SpiderPanel Status"
    echo "========================================"

    echo "OS:          $OS_NAME"
    echo "Version:     $OS_VERSION"
    echo "Codespace:   $IS_CODESPACE"
    echo "Systemd:     $HAS_SYSTEMD"
    echo "App Dir:     $APP_DIR"
    echo "Port:        $PORT"
    echo "Python:      $("$VENV_DIR/bin/python" --version 2>&1 || echo "missing")"

    if [[ "$HAS_SYSTEMD" == "true" ]]; then
        echo "Service:     $(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo inactive)"
    else
        if is_running_standalone; then
            echo "Process:     running ($(cat "$PID_FILE"))"
        else
            echo "Process:     stopped"
        fi
    fi

    echo "========================================"
    echo
}

# ------------------------------------------------------------
# Public / local address detection
# ------------------------------------------------------------

get_local_ip() {

    local IP=""

    if command -v hostname >/dev/null 2>&1; then
        IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    fi

    if [[ -z "$IP" ]] && command -v ip >/dev/null 2>&1; then
        IP="$(
            ip route get 1.1.1.1 2>/dev/null \
            | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src") print $(i+1); exit}' \
            || true
        )"
    fi

    echo "${IP:-127.0.0.1}"
}

get_public_ip() {

    local IP=""

    IP="$(curl -4 -fsSL --max-time 5 https://api.ipify.org 2>/dev/null || true)"

    if [[ -z "$IP" ]]; then
        IP="$(curl -4 -fsSL --max-time 5 https://ifconfig.me 2>/dev/null || true)"
    fi

    echo "${IP:-Unavailable}"
}

get_codespace_url() {

    local DOMAIN=""
    local NAME=""

    NAME="${CODESPACE_NAME:-}"

    if [[ -z "$NAME" ]]; then
        echo "Unavailable"
        return
    fi

    DOMAIN="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"

    echo "https://${NAME}-${PORT}.${DOMAIN}/spider"
}

# ------------------------------------------------------------
# Info
# ------------------------------------------------------------

info_panel() {

    local PASSWORD=""

    if [[ -f "$ENV_FILE" ]]; then
        PASSWORD="$(
            grep '^ADMIN_PASSWORD=' "$ENV_FILE" \
            | head -n1 \
            | cut -d= -f2- \
            || true
        )
    fi

    local LOCAL_IP
    local PUBLIC_IP

    LOCAL_IP="$(get_local_ip)"
    PUBLIC_IP="$(get_public_ip)"

    echo
    echo "======================================================"
    echo "                 SPIDERPANEL"
    echo "======================================================"
    echo
    echo "Mode:"
    if [[ "$HAS_SYSTEMD" == "true" ]]; then
        echo "  VPS / systemd"
    else
        if [[ "$IS_CODESPACE" == "true" ]]; then
            echo "  GitHub Codespaces / standalone"
        else
            echo "  Standalone"
        fi
    fi

    echo
    echo "Application:"
    echo "  http://127.0.0.1:$PORT/spider"

    echo "Local IP:"
    echo "  http://${LOCAL_IP}:$PORT/spider"

    echo "Public IP:"
    echo "  $PUBLIC_IP"

    if [[ "$IS_CODESPACE" == "true" ]]; then
        echo
        echo "Codespace URL:"
        echo "  $(get_codespace_url)"
        echo
        echo "GitHub Codespaces:"
        echo "  Forward TCP port $PORT in the Ports tab."
    fi

    echo
    echo "Admin Password:"
    echo "  ${PASSWORD:-Unavailable}"

    echo
    echo "Credentials:"
    echo "  $APP_DIR/INSTALL-CREDENTIALS.txt"

    echo
    echo "Environment:"
    echo "  $ENV_FILE"

    echo
    echo "Python:"
    echo "  $VENV_DIR/bin/python"

    echo
    echo "Xray:"
    if [[ -x "$XRAY_BIN" ]]; then
        echo "  $XRAY_BIN"
        "$XRAY_BIN" version 2>/dev/null | head -n 1 || true
    else
        echo "  Not installed"
    fi

    echo
    echo "MTProxy:"
    if [[ -x "$MTPROXY_BIN" ]]; then
        echo "  $MTPROXY_BIN"
    else
        echo "  Not installed"
    fi

    echo
    echo "Commands:"
    echo "  spiderpanel"
    echo "  spiderpanel info"
    echo "  spiderpanel status"
    echo "  spiderpanel start"
    echo "  spiderpanel stop"
    echo "  spiderpanel restart"
    echo "  spiderpanel update"
    echo "  spiderpanel logs"
    echo "  spiderpanel uninstall"

    echo
    echo "======================================================"
    echo
}

# ------------------------------------------------------------
# Logs
# ------------------------------------------------------------

logs_panel() {

    if [[ "$HAS_SYSTEMD" == "true" ]]; then
        journalctl -u "$SERVICE_NAME" -f --no-pager
        return
    fi

    if [[ -f "$LOG_FILE" ]]; then
        tail -n 200 -f "$LOG_FILE"
    else
        warn "No log file found."
    fi
}

# ------------------------------------------------------------
# Update
# ------------------------------------------------------------

update_panel() {

    log "Updating SpiderPanel..."

    local TMP_INSTALLER
    TMP_INSTALLER="$(mktemp /tmp/spiderpanel-update.XXXXXX.sh)"

    if ! curl -fsSL "$INSTALLER_URL" -o "$TMP_INSTALLER"; then
        rm -f "$TMP_INSTALLER"
        fail "Could not download updated installer."
    fi

    chmod 700 "$TMP_INSTALLER"

    bash "$TMP_INSTALLER" install

    rm -f "$TMP_INSTALLER"
}

# ------------------------------------------------------------
# Uninstall
# ------------------------------------------------------------

uninstall_panel() {

    echo
    echo "WARNING: This will remove SpiderPanel."
    echo
    echo "Application directory:"
    echo "  $APP_DIR"
    echo
    echo "Systemd service:"
    echo "  $SERVICE_NAME"
    echo

    read -r -p "Type REMOVE to continue: " CONFIRM

    if [[ "$CONFIRM" != "REMOVE" ]]; then
        echo "Cancelled."
        return 0
    fi

    if [[ "$HAS_SYSTEMD" == "true" ]]; then
        systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
        systemctl daemon-reload || true
    else
        stop_standalone || true
    fi

    rm -f "$CLI_PATH"
    rm -f "$ENV_FILE"

    # Remove app but preserve nothing intentionally.
    rm -rf "$APP_DIR"

    ok "SpiderPanel removed."
}

# ------------------------------------------------------------
# Global CLI
# ------------------------------------------------------------

create_cli() {

    cat > "$CLI_PATH" <<'EOF'
#!/usr/bin/env bash

set -e

APP_DIR="/opt/SpiderPanel"
INSTALLER_URL="https://raw.githubusercontent.com/amirh00sain/SpiderPanel/main/start.sh"

case "${1:-menu}" in

    install)
        exec bash "$APP_DIR/start.sh" install
        ;;

    info)
        exec bash "$APP_DIR/start.sh" info
        ;;

    status)
        exec bash "$APP_DIR/start.sh" status
        ;;

    start)
        exec bash "$APP_DIR/start.sh" start
        ;;

    stop)
        exec bash "$APP_DIR/start.sh" stop
        ;;

    restart)
        exec bash "$APP_DIR/start.sh" restart
        ;;

    update)

        TMP_INSTALLER="$(mktemp /tmp/spiderpanel-cli-update.XXXXXX.sh)"

        curl -fsSL "$INSTALLER_URL" -o "$TMP_INSTALLER"

        chmod 700 "$TMP_INSTALLER"

        sudo bash "$TMP_INSTALLER" install

        rm -f "$TMP_INSTALLER"
        ;;

    logs)
        exec bash "$APP_DIR/start.sh" logs
        ;;

    uninstall)
        exec bash "$APP_DIR/start.sh" uninstall
        ;;

    *)
        echo
        echo "SpiderPanel"
        echo
        echo "1) info"
        echo "2) status"
        echo "3) start"
        echo "4) stop"
        echo "5) restart"
        echo "6) update"
        echo "7) logs"
        echo "8) uninstall"
        echo "0) exit"
        echo

        read -r -p "Select: " CHOICE

        case "$CHOICE" in
            1) exec bash "$APP_DIR/start.sh" info ;;
            2) exec bash "$APP_DIR/start.sh" status ;;
            3) exec bash "$APP_DIR/start.sh" start ;;
            4) exec bash "$APP_DIR/start.sh" stop ;;
            5) exec bash "$APP_DIR/start.sh" restart ;;
            6) exec bash "$APP_DIR/start.sh" update ;;
            7) exec bash "$APP_DIR/start.sh" logs ;;
            8) exec bash "$APP_DIR/start.sh" uninstall ;;
            0) exit 0 ;;
            *) echo "Invalid selection." ;;
        esac
        ;;
esac
EOF

    chmod +x "$CLI_PATH"

    ok "Global command installed: spiderpanel"
}

# ------------------------------------------------------------
# Main install
# ------------------------------------------------------------

install_panel() {

    detect_os
    detect_environment

    log "Detected OS: $OS_NAME"
    log "Detected environment: Codespace=$IS_CODESPACE Systemd=$HAS_SYSTEMD"

    install_base_packages

    install_docker || true

    download_repository
    deploy_application

    # IMPORTANT:
    # Copy current installer into application dir so
    # `spiderpanel update` works locally too.
    if [[ -f "$TMP_ROOT/app/start.sh" ]]; then
        cp "$TMP_ROOT/app/start.sh" "$APP_DIR/start.sh"
        chmod +x "$APP_DIR/start.sh"
    fi

    setup_python

    install_xray
    install_mtproxy

    setup_environment

    validate_application

    create_systemd_service

    create_cli

    start_panel

    info_panel

    ok "SpiderPanel installation completed successfully."
}

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

main() {

    # Help
    case "${1:-install}" in

        install)
            ensure_root "$@"
            install_panel
            ;;

        info)
            ensure_root "$@"
            detect_os
            detect_environment
            info_panel
            ;;

        status)
            ensure_root "$@"
            detect_os
            detect_environment
            status_panel
            ;;

        start)
            ensure_root "$@"
            detect_os
            detect_environment
            start_panel
            ;;

        stop)
            ensure_root "$@"
            detect_os
            detect_environment
            stop_panel
            ;;

        restart)
            ensure_root "$@"
            detect_os
            detect_environment
            restart_panel
            ;;

        update)
            ensure_root "$@"
            update_panel
            ;;

        logs)
            ensure_root "$@"
            detect_os
            detect_environment
            logs_panel
            ;;

        uninstall)
            ensure_root "$@"
            detect_os
            detect_environment
            uninstall_panel
            ;;

        *)
            echo
            echo "SpiderPanel Universal Installer"
            echo
            echo "Usage:"
            echo
            echo "  bash start.sh install"
            echo "  bash start.sh info"
            echo "  bash start.sh status"
            echo "  bash start.sh start"
            echo "  bash start.sh stop"
            echo "  bash start.sh restart"
            echo "  bash start.sh update"
            echo "  bash start.sh logs"
            echo "  bash start.sh uninstall"
            echo
            ;;
    esac
}

main "$@"
