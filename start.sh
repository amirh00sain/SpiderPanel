#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
#                    SpiderPanel Installer
# ============================================================

APP_NAME="SpiderPanel"

APP_DIR="${SPIDER_APP_DIR:-/opt/SpiderPanel}"
ENV_FILE="/etc/spider-panel.env"

REPO="${SPIDER_REPO:-https://github.com/amirh00sain/SpiderPanel.git}"
BRANCH="${SPIDER_BRANCH:-main}"

INSTALLER_URL="${SPIDER_INSTALLER_URL:-https://raw.githubusercontent.com/amirh00sain/SpiderPanel/main/start.sh}"

SERVICE_NAME="spider-panel"
PORT="8080"

VENV_DIR="$APP_DIR/.venv"
PID_FILE="$APP_DIR/spiderpanel.pid"
LOG_FILE="$APP_DIR/spiderpanel.log"

CLI_PATH="/usr/local/bin/spiderpanel"

UV_BIN="/usr/local/bin/uv"
UVX_BIN="/usr/local/bin/uvx"

XRAY_VERSION="26.3.27"
XRAY_DIR="$APP_DIR/xray"
XRAY_BIN="$XRAY_DIR/xray"

MTPROXY_BIN="/usr/local/bin/mtproto-proxy"

TMP_ROOT=""

# ============================================================
# Colors
# ============================================================

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

# ============================================================
# Cleanup
# ============================================================

cleanup() {
    if [[ -n "${TMP_ROOT:-}" && -d "${TMP_ROOT:-}" ]]; then
        rm -rf "$TMP_ROOT" >/dev/null 2>&1 || true
    fi
}

trap cleanup EXIT

# ============================================================
# Root
# ============================================================

ensure_root() {

    if [[ "$EUID" -eq 0 ]]; then
        return 0
    fi

    log "Root privileges required."
    log "Re-running installer with sudo..."

    local TMP_INSTALLER

    TMP_INSTALLER="$(mktemp /tmp/spiderpanel-start.XXXXXX.sh)"

    if ! curl -fsSL \
        --retry 5 \
        --retry-delay 2 \
        "$INSTALLER_URL" \
        -o "$TMP_INSTALLER"; then

        rm -f "$TMP_INSTALLER"
        fail "Unable to download installer for sudo execution."
    fi

    chmod 700 "$TMP_INSTALLER"

    exec sudo -E \
        env \
        HOME=/root \
        DEBIAN_FRONTEND=noninteractive \
        bash "$TMP_INSTALLER" "$@"
}

# ============================================================
# OS Detection
# ============================================================

OS_ID="unknown"
OS_NAME="unknown"
OS_VERSION="unknown"

detect_os() {

    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        source /etc/os-release

        OS_ID="${ID:-unknown}"
        OS_NAME="${NAME:-unknown}"
        OS_VERSION="${VERSION_ID:-unknown}"
    fi
}

# ============================================================
# Environment Detection
# ============================================================

IS_CODESPACE="false"
HAS_SYSTEMD="false"

detect_environment() {

    if [[ "${CODESPACES:-false}" == "true" ]] || \
       [[ -n "${CODESPACE_NAME:-}" ]]; then

        IS_CODESPACE="true"
    fi

    if command -v systemctl >/dev/null 2>&1; then

        if [[ -d /run/systemd/system ]] || \
           [[ "$(ps -p 1 -o comm= 2>/dev/null || true)" == "systemd" ]]; then

            HAS_SYSTEMD="true"
        fi
    fi
}

# ============================================================
# Packages
# ============================================================

install_apt_packages() {

    log "Installing Debian/Ubuntu dependencies..."

    apt-get update -y

    DEBIAN_FRONTEND=noninteractive \
    apt-get install -y \
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

    ok "System dependencies installed."
}

install_pacman_packages() {

    log "Installing Arch/Omarchy dependencies..."

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

    ok "System dependencies installed."
}

install_dnf_packages() {

    log "Installing Fedora/RHEL dependencies..."

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

    ok "System dependencies installed."
}

install_base_packages() {

    case "$OS_ID" in

        ubuntu|debian|linuxmint|pop)
            install_apt_packages
            ;;

        arch|manjaro|endeavouros)
            install_pacman_packages
            ;;

        fedora|rhel|centos|rocky|almalinux)
            install_dnf_packages
            ;;

        *)
            warn "Unsupported/unknown distribution: $OS_ID"
            warn "Continuing with currently installed tools."
            ;;
    esac
}

# ============================================================
# Optional Docker
# ============================================================

install_docker() {

    if command -v docker >/dev/null 2>&1; then
        ok "Docker already installed."
        return 0
    fi

    if [[ "$IS_CODESPACE" == "true" ]]; then
        log "GitHub Codespaces detected. Docker installation skipped."
        return 0
    fi

    log "Installing Docker..."

    if command -v apt-get >/dev/null 2>&1; then

        curl -fsSL \
            --retry 5 \
            https://get.docker.com \
            -o /tmp/get-docker.sh

        sh /tmp/get-docker.sh

        rm -f /tmp/get-docker.sh

    elif command -v pacman >/dev/null 2>&1; then

        pacman -S --noconfirm docker || true

    elif command -v dnf >/dev/null 2>&1; then

        dnf -y install \
            docker \
            docker-cli \
            containerd \
            docker-buildx-plugin \
            docker-compose-plugin || true
    fi

    if [[ "$HAS_SYSTEMD" == "true" ]] && \
       command -v systemctl >/dev/null 2>&1; then

        systemctl enable --now docker >/dev/null 2>&1 || true
    fi

    if command -v docker >/dev/null 2>&1; then
        ok "Docker installed."
    else
        warn "Docker installation was not completed."
    fi
}

# ============================================================
# Repository
# ============================================================

download_repository() {

    TMP_ROOT="$(mktemp -d /tmp/spiderpanel.XXXXXX)"

    log "Downloading SpiderPanel..."

    if ! git clone \
        --depth 1 \
        --branch "$BRANCH" \
        --single-branch \
        "$REPO" \
        "$TMP_ROOT/app"; then

        fail "Could not clone SpiderPanel repository."
    fi

    ok "Repository downloaded."
}

# ============================================================
# Deploy
# ============================================================

deploy_application() {

    mkdir -p "$APP_DIR"

    # Preserve data
    if [[ -d "$APP_DIR/data" ]]; then
        cp -a "$APP_DIR/data" "$TMP_ROOT/old-data" || true
    fi

    # Preserve local .env
    if [[ -f "$APP_DIR/.env" ]]; then
        cp "$APP_DIR/.env" "$TMP_ROOT/old.env" || true
    fi

    log "Installing application files..."

    rsync -a \
        --delete \
        --exclude ".git/" \
        --exclude ".venv/" \
        --exclude "data/" \
        --exclude "*.pid" \
        --exclude "*.log" \
        "$TMP_ROOT/app/" \
        "$APP_DIR/"

    # Restore data
    if [[ -d "$TMP_ROOT/old-data" ]]; then
        mkdir -p "$APP_DIR/data"
        rsync -a \
            "$TMP_ROOT/old-data/" \
            "$APP_DIR/data/" || true
    fi

    # Restore .env
    if [[ -f "$TMP_ROOT/old.env" ]]; then
        cp "$TMP_ROOT/old.env" "$APP_DIR/.env"
    fi

    chmod +x "$APP_DIR/start.sh" 2>/dev/null || true

    ok "Application installed."
}

# ============================================================
# UV INSTALLATION
#
# IMPORTANT:
# Does NOT use astral.sh installation script.
# Downloads official GitHub release directly.
# ============================================================

install_uv() {

    log "Preparing dedicated Python 3.12 runtime..."

    if [[ -x "$UV_BIN" ]]; then

        if "$UV_BIN" --version >/dev/null 2>&1; then
            ok "uv already installed: $("$UV_BIN" --version)"
            return 0
        fi
    fi

    local ARCH
    local UV_ARCH
    local UV_VERSION="0.12.9"
    local TMP_UV
    local ARCHIVE
    local URL
    local UV_SOURCE
    local UVX_SOURCE

    ARCH="$(uname -m)"

    case "$ARCH" in

        x86_64)
            UV_ARCH="x86_64-unknown-linux-gnu"
            ;;

        aarch64|arm64)
            UV_ARCH="aarch64-unknown-linux-gnu"
            ;;

        armv7l)
            UV_ARCH="armv7-unknown-linux-gnueabihf"
            ;;

        *)
            fail "Unsupported architecture for uv: $ARCH"
            ;;
    esac

    TMP_UV="$(mktemp -d /tmp/spiderpanel-uv.XXXXXX)"
    ARCHIVE="$TMP_UV/uv.tar.gz"

    URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${UV_ARCH}.tar.gz"

    log "Downloading uv ${UV_VERSION}..."

    if ! curl -fL \
        --retry 5 \
        --retry-delay 2 \
        --connect-timeout 20 \
        "$URL" \
        -o "$ARCHIVE"; then

        rm -rf "$TMP_UV"
        fail "Could not download uv."
    fi

    log "Extracting uv..."

    if ! tar -xzf "$ARCHIVE" -C "$TMP_UV"; then

        rm -rf "$TMP_UV"
        fail "Could not extract uv archive."
    fi

    UV_SOURCE="$(
        find "$TMP_UV" \
            -type f \
            -name uv \
            -perm -u+x \
            2>/dev/null \
        | head -n1 || true
    )"

    UVX_SOURCE="$(
        find "$TMP_UV" \
            -type f \
            -name uvx \
            -perm -u+x \
            2>/dev/null \
        | head -n1 || true
    )"

    if [[ -z "$UV_SOURCE" ]]; then

        rm -rf "$TMP_UV"
        fail "uv binary was not found in downloaded archive."
    fi

    log "Installing uv to /usr/local/bin..."

    install -m 0755 "$UV_SOURCE" "$UV_BIN"

    if [[ -n "$UVX_SOURCE" ]]; then
        install -m 0755 "$UVX_SOURCE" "$UVX_BIN"
    fi

    rm -rf "$TMP_UV"

    hash -r 2>/dev/null || true

    if [[ ! -x "$UV_BIN" ]]; then
        fail "uv installation failed."
    fi

    if ! "$UV_BIN" --version >/dev/null 2>&1; then
        fail "uv was installed but cannot execute."
    fi

    ok "uv installed successfully: $("$UV_BIN" --version)"
}

# ============================================================
# Python 3.12
# ============================================================

setup_python() {

    install_uv

    local UV="$UV_BIN"
    local PY312=""
    local PY312_VERSION=""
    local PYTHON_BIN="$VENV_DIR/bin/python"

    log "Installing Python 3.12 with uv..."

    if ! "$UV" python install 3.12; then
        fail "Could not install Python 3.12."
    fi

    PY312="$(
        "$UV" python find 3.12 2>/dev/null \
        | head -n1 || true
    )"

    if [[ -z "$PY312" ]]; then
        fail "Could not find Python 3.12."
    fi

    if [[ ! -x "$PY312" ]]; then
        fail "Python 3.12 binary is not executable: $PY312"
    fi

    PY312_VERSION="$(
        "$PY312" -c \
        'import sys; print(".".join(map(str, sys.version_info[:3])))'
    )"

    case "$PY312_VERSION" in
        3.12.*)
            ;;
        *)
            fail "Wrong Python version detected: $PY312_VERSION"
            ;;
    esac

    ok "Python selected: $PY312 ($PY312_VERSION)"

    # --------------------------------------------------------
    # Delete old 3.14 virtualenv
    # --------------------------------------------------------

    if [[ -x "$PYTHON_BIN" ]]; then

        local OLD_VERSION

        OLD_VERSION="$(
            "$PYTHON_BIN" -c \
            'import sys; print(".".join(map(str,sys.version_info[:2])))' \
            2>/dev/null || true
        )"

        if [[ "$OLD_VERSION" != "3.12" ]]; then

            warn "Old .venv uses Python $OLD_VERSION."
            log "Removing old virtual environment..."

            rm -rf "$VENV_DIR"
        fi
    fi

    # --------------------------------------------------------
    # Always recreate venv using Python 3.12
    # --------------------------------------------------------

    log "Creating Python 3.12 virtual environment..."

    rm -rf "$VENV_DIR"

    if ! "$UV" venv \
        --python "$PY312" \
        "$VENV_DIR"; then

        fail "Failed to create Python 3.12 virtual environment."
    fi

    if [[ ! -x "$PYTHON_BIN" ]]; then
        fail "Virtual environment Python was not created."
    fi

    local FINAL_VERSION

    FINAL_VERSION="$(
        "$PYTHON_BIN" -c \
        'import sys; print(".".join(map(str,sys.version_info[:3])))'
    )"

    case "$FINAL_VERSION" in
        3.12.*)
            ;;
        *)
            fail "Virtual environment has wrong Python: $FINAL_VERSION"
            ;;
    esac

    ok "Virtual environment ready: Python $FINAL_VERSION"

    # --------------------------------------------------------
    # Packaging tools
    # --------------------------------------------------------

    log "Upgrading pip, setuptools and wheel..."

    "$PYTHON_BIN" -m pip install \
        --upgrade \
        --no-cache-dir \
        pip \
        setuptools \
        wheel

    # --------------------------------------------------------
    # Dependencies
    # --------------------------------------------------------

    if [[ ! -f "$APP_DIR/requirements.txt" ]]; then
        fail "requirements.txt not found."
    fi

    log "Installing SpiderPanel dependencies..."

    "$PYTHON_BIN" -m pip install \
        --no-cache-dir \
        -r "$APP_DIR/requirements.txt"

    # --------------------------------------------------------
    # Test imports
    # --------------------------------------------------------

    log "Testing Python dependencies..."

    "$PYTHON_BIN" - <<'PY'
import sys

assert sys.version_info[:2] == (3, 12), sys.version

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

print("Python:", sys.version)
print("FastAPI:", fastapi.__version__)
print("Uvicorn:", uvicorn.__version__)
print("HTTPX:", httpx.__version__)
print("Pillow:", PIL.__version__)
print("Dependencies: OK")
PY

    ok "Python 3.12 environment completed."
}

# ============================================================
# Xray
# ============================================================

install_xray() {

    mkdir -p "$XRAY_DIR"

    if [[ -x "$XRAY_BIN" ]]; then

        if "$XRAY_BIN" version >/dev/null 2>&1; then
            ok "Xray already installed."
            return 0
        fi
    fi

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

    log "Downloading Xray ${XRAY_VERSION}..."

    if ! curl -fL \
        --retry 5 \
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
        warn "Xray extraction failed."
        return 0
    fi

    rm -f "$ZIP"

    if [[ -f "$XRAY_BIN" ]]; then

        chmod +x "$XRAY_BIN"

        if "$XRAY_BIN" version >/dev/null 2>&1; then
            ok "Xray installed."
        else
            warn "Xray binary failed validation."
        fi
    else
        warn "Xray binary was not found."
    fi
}

# ============================================================
# MTProxy
# ============================================================

install_mtproxy() {

    if [[ -x "$MTPROXY_BIN" ]]; then
        ok "MTProxy already installed."
        return 0
    fi

    if ! command -v git >/dev/null 2>&1; then
        warn "Git is unavailable. MTProxy skipped."
        return 0
    fi

    local TMP_MT
    local MT_BIN

    TMP_MT="$(mktemp -d /tmp/spiderpanel-mtproxy.XXXXXX)"

    log "Downloading Telegram MTProxy..."

    if ! git clone \
        --depth 1 \
        https://github.com/TelegramMessenger/MTProxy.git \
        "$TMP_MT/MTProxy"; then

        rm -rf "$TMP_MT"
        warn "MTProxy download failed."
        return 0
    fi

    log "Building MTProxy..."

    if ! make \
        -C "$TMP_MT/MTProxy" \
        -j"$(nproc 2>/dev/null || echo 2)"; then

        rm -rf "$TMP_MT"
        warn "MTProxy build failed."
        return 0
    fi

    MT_BIN=""

    if [[ -x "$TMP_MT/MTProxy/objs/bin/mtproto-proxy" ]]; then
        MT_BIN="$TMP_MT/MTProxy/objs/bin/mtproto-proxy"
    elif [[ -x "$TMP_MT/MTProxy/mtproto-proxy" ]]; then
        MT_BIN="$TMP_MT/MTProxy/mtproto-proxy"
    fi

    if [[ -z "$MT_BIN" ]]; then

        rm -rf "$TMP_MT"
        warn "MTProxy binary was not found."
        return 0
    fi

    install -m 0755 \
        "$MT_BIN" \
        "$MTPROXY_BIN"

    rm -rf "$TMP_MT"

    if [[ -x "$MTPROXY_BIN" ]]; then
        ok "MTProxy installed."
    else
        warn "MTProxy installation failed."
    fi
}

# ============================================================
# Password / Secret
# ============================================================

random_hex() {

    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
        return
    fi

    if [[ -r /dev/urandom ]]; then
        od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
        return
    fi

    date +%s%N
}

generate_password() {

    random_hex | head -c 20
}

# ============================================================
# Environment
# ============================================================

setup_environment() {

    log "Configuring SpiderPanel environment..."

    mkdir -p "$APP_DIR/data"

    local SECRET_KEY=""
    local ADMIN_PASSWORD=""

    if [[ -f "$ENV_FILE" ]]; then

        SECRET_KEY="$(
            grep '^SECRET_KEY=' "$ENV_FILE" \
            | head -n1 \
            | cut -d= -f2- \
            || true
        )

        ADMIN_PASSWORD="$(
            grep '^ADMIN_PASSWORD=' "$ENV_FILE" \
            | head -n1 \
            | cut -d= -f2- \
            || true
        )
    fi

    [[ -n "$SECRET_KEY" ]] || SECRET_KEY="$(random_hex)"
    [[ -n "$ADMIN_PASSWORD" ]] || ADMIN_PASSWORD="$(generate_password)"

    cat > "$ENV_FILE" <<EOF
SECRET_KEY=$SECRET_KEY
ADMIN_PASSWORD=$ADMIN_PASSWORD

PORT=8080

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

    cat > "$APP_DIR/INSTALL-CREDENTIALS.txt" <<EOF
========================================
              SpiderPanel
========================================

Application:
  http://127.0.0.1:8080/spider

Port:
  8080

Admin Password:
  $ADMIN_PASSWORD

Application Directory:
  $APP_DIR

Environment:
  $ENV_FILE

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

# ============================================================
# Validation
# ============================================================

validate_application() {

    log "Validating SpiderPanel..."

    local PYTHON="$VENV_DIR/bin/python"

    [[ -x "$PYTHON" ]] || \
        fail "Python virtual environment missing."

    if [[ -f "$APP_DIR/main.py" ]]; then
        "$PYTHON" -m py_compile "$APP_DIR/main.py"
    else
        warn "main.py was not found in repository root."
    fi

    "$PYTHON" -m compileall -q "$APP_DIR"

    ok "Application validation completed."
}

# ============================================================
# Systemd Service
# ============================================================

create_systemd_service() {

    [[ "$HAS_SYSTEMD" == "true" ]] || return 0

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

ExecStart=$VENV_DIR/bin/uvicorn main:app --host 0.0.0.0 --port 8080

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

# ============================================================
# Standalone
# ============================================================

is_running_standalone() {

    [[ -f "$PID_FILE" ]] || return 1

    local PID

    PID="$(cat "$PID_FILE" 2>/dev/null || true)"

    [[ -n "$PID" ]] || return 1

    kill -0 "$PID" >/dev/null 2>&1
}

start_standalone() {

    if is_running_standalone; then
        ok "SpiderPanel is already running."
        return 0
    fi

    local UVICORN="$VENV_DIR/bin/uvicorn"

    [[ -x "$UVICORN" ]] || \
        fail "uvicorn was not installed."

    mkdir -p "$APP_DIR"

    touch "$LOG_FILE"

    log "Starting SpiderPanel in standalone mode..."

    cd "$APP_DIR"

    nohup "$UVICORN" \
        main:app \
        --host 0.0.0.0 \
        --port 8080 \
        >> "$LOG_FILE" 2>&1 &

    echo $! > "$PID_FILE"

    sleep 3

    if is_running_standalone; then
        ok "SpiderPanel started."
    else
        error "SpiderPanel failed to start."
        tail -n 100 "$LOG_FILE" 2>/dev/null || true
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

# ============================================================
# Start
# ============================================================

start_panel() {

    if [[ "$HAS_SYSTEMD" == "true" ]]; then

        create_systemd_service

        log "Starting SpiderPanel with systemd..."

        systemctl restart "$SERVICE_NAME"

        sleep 4

        if systemctl is-active --quiet "$SERVICE_NAME"; then
            ok "SpiderPanel is running."
        else
            error "SpiderPanel failed to start."
            journalctl \
                -u "$SERVICE_NAME" \
                -n 100 \
                --no-pager || true

            return 1
        fi

    else

        start_standalone
    fi
}

# ============================================================
# Stop
# ============================================================

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

# ============================================================
# Restart
# ============================================================

restart_panel() {

    stop_panel || true

    sleep 1

    start_panel
}

# ============================================================
# Status
# ============================================================

status_panel() {

    echo
    echo "======================================================"
    echo "                 SpiderPanel Status"
    echo "======================================================"

    echo "OS:          $OS_NAME"
    echo "Version:     $OS_VERSION"
    echo "Codespace:   $IS_CODESPACE"
    echo "Systemd:     $HAS_SYSTEMD"
    echo "App Dir:     $APP_DIR"
    echo "Port:        8080"

    if [[ -x "$VENV_DIR/bin/python" ]]; then
        echo "Python:      $("$VENV_DIR/bin/python" --version 2>&1)"
    else
        echo "Python:      Missing"
    fi

    if [[ "$HAS_SYSTEMD" == "true" ]]; then
        echo "Service:     $(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo inactive)"
    else
        if is_running_standalone; then
            echo "Process:     running ($(cat "$PID_FILE"))"
        else
            echo "Process:     stopped"
        fi
    fi

    echo "======================================================"
    echo
}

# ============================================================
# IP detection
# ============================================================

get_local_ip() {

    local IP=""

    if command -v hostname >/dev/null 2>&1; then

        IP="$(
            hostname -I 2>/dev/null \
            | awk '{print $1}' \
            || true
        )"
    fi

    if [[ -z "$IP" ]] && command -v ip >/dev/null 2>&1; then

        IP="$(
            ip route get 1.1.1.1 2>/dev/null \
            | awk '/src/ {
                for(i=1;i<=NF;i++)
                    if($i=="src") {
                        print $(i+1)
                        exit
                    }
            }' \
            || true
        )"
    fi

    echo "${IP:-127.0.0.1}"
}

get_public_ip() {

    local IP=""

    IP="$(
        curl -4 -fsSL \
        --connect-timeout 5 \
        --max-time 10 \
        https://api.ipify.org \
        2>/dev/null \
        || true
    )"

    if [[ -z "$IP" ]]; then

        IP="$(
            curl -4 -fsSL \
            --connect-timeout 5 \
            --max-time 10 \
            https://ifconfig.me \
            2>/dev/null \
            || true
        )"
    fi

    echo "${IP:-Unavailable}"
}

get_codespace_url() {

    if [[ -z "${CODESPACE_NAME:-}" ]]; then
        echo "Unavailable"
        return
    fi

    local DOMAIN

    DOMAIN="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"

    echo "https://${CODESPACE_NAME}-${PORT}.${DOMAIN}/spider"
}

# ============================================================
# Info
# ============================================================

info_panel() {

    local PASSWORD=""
    local LOCAL_IP
    local PUBLIC_IP

    if [[ -f "$ENV_FILE" ]]; then

        PASSWORD="$(
            grep '^ADMIN_PASSWORD=' "$ENV_FILE" \
            | head -n1 \
            | cut -d= -f2- \
            || true
        )
    fi

    LOCAL_IP="$(get_local_ip)"
    PUBLIC_IP="$(get_public_ip)"

    echo
    echo "======================================================"
    echo "                  SPIDERPANEL"
    echo "======================================================"
    echo

    if [[ "$HAS_SYSTEMD" == "true" ]]; then
        echo "Mode:"
        echo "  VPS / systemd"
    elif [[ "$IS_CODESPACE" == "true" ]]; then
        echo "Mode:"
        echo "  GitHub Codespaces / standalone"
    else
        echo "Mode:"
        echo "  Standalone"
    fi

    echo
    echo "Application:"
    echo "  http://127.0.0.1:8080/spider"

    echo
    echo "Local:"
    echo "  http://${LOCAL_IP}:8080/spider"

    echo
    echo "Public IP:"
    echo "  $PUBLIC_IP"

    if [[ "$IS_CODESPACE" == "true" ]]; then

        echo
        echo "Codespace:"
        echo "  $(get_codespace_url)"

        echo
        echo "IMPORTANT:"
        echo "  Forward port 8080 from the Codespaces Ports tab."
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
        "$XRAY_BIN" version 2>/dev/null | head -n1 || true
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

# ============================================================
# Logs
# ============================================================

logs_panel() {

    if [[ "$HAS_SYSTEMD" == "true" ]]; then

        journalctl \
            -u "$SERVICE_NAME" \
            -f \
            --no-pager

    else

        if [[ -f "$LOG_FILE" ]]; then
            tail -n 200 -f "$LOG_FILE"
        else
            warn "No SpiderPanel log found."
        fi
    fi
}

# ============================================================
# Update
# ============================================================

update_panel() {

    log "Updating SpiderPanel..."

    local TMP_INSTALLER

    TMP_INSTALLER="$(mktemp /tmp/spiderpanel-update.XXXXXX.sh)"

    if ! curl -fsSL \
        --retry 5 \
        --retry-delay 2 \
        "$INSTALLER_URL" \
        -o "$TMP_INSTALLER"; then

        rm -f "$TMP_INSTALLER"
        fail "Could not download updated installer."
    fi

    chmod 700 "$TMP_INSTALLER"

    bash "$TMP_INSTALLER" install

    rm -f "$TMP_INSTALLER"
}

# ============================================================
# Uninstall
# ============================================================

uninstall_panel() {

    echo
    echo "======================================================"
    echo " WARNING: SpiderPanel will be removed."
    echo "======================================================"
    echo
    echo "Application:"
    echo "  $APP_DIR"
    echo
    echo "Environment:"
    echo "  $ENV_FILE"
    echo

    read -r -p "Type REMOVE to continue: " CONFIRM

    if [[ "$CONFIRM" != "REMOVE" ]]; then
        echo
        echo "Cancelled."
        echo
        return 0
    fi

    if [[ "$HAS_SYSTEMD" == "true" ]]; then

        systemctl disable \
            --now \
            "$SERVICE_NAME" \
            >/dev/null 2>&1 || true

        rm -f \
            "/etc/systemd/system/${SERVICE_NAME}.service"

        systemctl daemon-reload || true

    else

        stop_standalone || true
    fi

    rm -f "$CLI_PATH"
    rm -f "$ENV_FILE"

    rm -rf "$APP_DIR"

    ok "SpiderPanel completely removed."
}

# ============================================================
# Global CLI
# ============================================================

create_cli() {

    cat > "$CLI_PATH" <<'EOF'
#!/usr/bin/env bash

set -e

APP_DIR="/opt/SpiderPanel"
INSTALLER_URL="https://raw.githubusercontent.com/amirh00sain/SpiderPanel/main/start.sh"

run_panel() {
    exec bash "$APP_DIR/start.sh" "$@"
}

case "${1:-menu}" in

    info)
        run_panel info
        ;;

    status)
        run_panel status
        ;;

    start)
        run_panel start
        ;;

    stop)
        run_panel stop
        ;;

    restart)
        run_panel restart
        ;;

    logs)
        run_panel logs
        ;;

    install)
        run_panel install
        ;;

    update)

        TMP_INSTALLER="$(mktemp /tmp/spiderpanel-cli-update.XXXXXX.sh)"

        curl -fsSL \
            --retry 5 \
            --retry-delay 2 \
            "$INSTALLER_URL" \
            -o "$TMP_INSTALLER"

        chmod 700 "$TMP_INSTALLER"

        sudo bash "$TMP_INSTALLER" install

        rm -f "$TMP_INSTALLER"
        ;;

    uninstall)
        run_panel uninstall
        ;;

    *)

        echo
        echo "================================================"
        echo "              SpiderPanel Manager"
        echo "================================================"
        echo
        echo "1) Info"
        echo "2) Status"
        echo "3) Start"
        echo "4) Stop"
        echo "5) Restart"
        echo "6) Update"
        echo "7) Logs"
        echo "8) Uninstall"
        echo "0) Exit"
        echo

        read -r -p "Select: " CHOICE

        case "$CHOICE" in
            1) run_panel info ;;
            2) run_panel status ;;
            3) run_panel start ;;
            4) run_panel stop ;;
            5) run_panel restart ;;
            6) run_panel update ;;
            7) run_panel logs ;;
            8) run_panel uninstall ;;
            0) exit 0 ;;
            *) echo "Invalid selection." ;;
        esac

        ;;
esac
EOF

    chmod +x "$CLI_PATH"

    ok "Global command installed: spiderpanel"
}

# ============================================================
# Full Installation
# ============================================================

install_panel() {

    detect_os
    detect_environment

    echo
    echo "======================================================"
    echo "                SpiderPanel Installer"
    echo "======================================================"
    echo
    echo "OS:        $OS_NAME"
    echo "Version:   $OS_VERSION"
    echo "Codespace: $IS_CODESPACE"
    echo "Systemd:   $HAS_SYSTEMD"
    echo "Port:      8080"
    echo
    echo "======================================================"
    echo

    install_base_packages

    install_docker || true

    download_repository

    deploy_application

    setup_python

    install_xray || true

    install_mtproxy || true

    setup_environment

    validate_application

    create_systemd_service

    create_cli

    start_panel

    info_panel

    ok "SpiderPanel installation completed."
}

# ============================================================
# Main
# ============================================================

main() {

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
