#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# SpiderPanel Universal Installer
# ============================================================

APP_NAME="SpiderPanel"

APP_DIR="${SPIDER_APP_DIR:-/opt/SpiderPanel}"
ENV_FILE="/etc/spider-panel.env"

REPO="${SPIDER_REPO:-https://github.com/amirh00sain/SpiderPanel.git}"
BRANCH="${SPIDER_BRANCH:-main}"
INSTALLER_URL="${SPIDER_INSTALLER_URL:-https://raw.githubusercontent.com/amirh00sain/SpiderPanel/main/start.sh}"

PORT="8080"

SERVICE_NAME="spider-panel"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

VENV_DIR="$APP_DIR/.venv"
PID_FILE="$APP_DIR/spiderpanel.pid"
LOG_FILE="$APP_DIR/spiderpanel.log"
CREDENTIALS_FILE="$APP_DIR/INSTALL-CREDENTIALS.txt"

CLI_PATH="/usr/local/bin/spiderpanel"

UV_VERSION="0.12.9"
UV_BIN="/usr/local/bin/uv"
UVX_BIN="/usr/local/bin/uvx"

XRAY_VERSION="${XRAY_VERSION:-26.3.27}"
XRAY_DIR="$APP_DIR/xray"
XRAY_BIN="$XRAY_DIR/xray"

MTPROXY_BIN="/usr/local/bin/mtproto-proxy"

TMP_ROOT=""
TMP_INSTALLER=""

OS_ID="unknown"
OS_NAME="unknown"
OS_VERSION="unknown"

IS_CODESPACE=0
HAS_SYSTEMD=0

# ============================================================
# Colors
# ============================================================

if [[ -t 1 ]]; then
    RED="\033[1;31m"
    GREEN="\033[1;32m"
    YELLOW="\033[1;33m"
    CYAN="\033[1;36m"
    BLUE="\033[1;34m"
    NC="\033[0m"
else
    RED=""
    GREEN=""
    YELLOW=""
    CYAN=""
    BLUE=""
    NC=""
fi

# ============================================================
# Logging
# ============================================================

log() {
    printf "${CYAN}[SpiderPanel]${NC} %s\n" "$*"
}

ok() {
    printf "${GREEN}[OK]${NC} %s\n" "$*"
}

warn() {
    printf "${YELLOW}[WARN]${NC} %s\n" "$*" >&2
}

error() {
    printf "${RED}[ERROR]${NC} %s\n" "$*" >&2
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

    if [[ -n "${TMP_INSTALLER:-}" && -f "${TMP_INSTALLER:-}" ]]; then
        rm -f "$TMP_INSTALLER" >/dev/null 2>&1 || true
    fi
}

trap cleanup EXIT

# ============================================================
# Root escalation
# ============================================================

become_root() {

    if [[ "$(id -u)" -eq 0 ]]; then
        return 0
    fi

    command -v sudo >/dev/null 2>&1 || \
        fail "sudo is not installed."

    TMP_INSTALLER="$(mktemp /tmp/spiderpanel-start.XXXXXX.sh)" || \
        fail "Could not create temporary installer."

    log "Downloading installer for automatic sudo execution..."

    curl -fsSL \
        --retry 5 \
        --retry-delay 2 \
        --connect-timeout 15 \
        --max-time 300 \
        "$INSTALLER_URL" \
        -o "$TMP_INSTALLER" || \
        fail "Could not download installer."

    chmod 700 "$TMP_INSTALLER"

    export SPIDER_APP_DIR="$APP_DIR"
    export SPIDER_REPO="$REPO"
    export SPIDER_BRANCH="$BRANCH"
    export SPIDER_INSTALLER_URL="$INSTALLER_URL"
    export SPIDER_PORT="$PORT"
    export XRAY_VERSION="$XRAY_VERSION"

    exec sudo -E bash "$TMP_INSTALLER" "$@"
}

# ============================================================
# OS Detection
# ============================================================

detect_os() {

    [[ -f /etc/os-release ]] || \
        fail "/etc/os-release not found."

    # shellcheck disable=SC1091
    source /etc/os-release

    OS_ID="${ID:-unknown}"
    OS_NAME="${NAME:-unknown}"
    OS_VERSION="${VERSION_ID:-unknown}"
}

# ============================================================
# Environment Detection
# ============================================================

detect_environment() {

    IS_CODESPACE=0
    HAS_SYSTEMD=0

    if [[ "${CODESPACES:-}" == "true" ]] || \
       [[ -n "${CODESPACE_NAME:-}" ]]; then

        IS_CODESPACE=1
    fi

    if command -v systemctl >/dev/null 2>&1; then

        local PID1

        PID1="$(
            ps -p 1 -o comm= 2>/dev/null |
            tr -d '[:space:]' ||
            true
        )"

        if [[ "$PID1" == "systemd" ]]; then
            HAS_SYSTEMD=1
        fi
    fi
}

# ============================================================
# Packages
# ============================================================

install_apt() {

    export DEBIAN_FRONTEND=noninteractive

    log "Installing Debian/Ubuntu dependencies..."

    apt-get update -y

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

install_dnf() {

    log "Installing Fedora/RHEL dependencies..."

    dnf install -y \
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

install_pacman() {

    log "Installing Arch/Omarchy dependencies..."

    pacman -Sy --noconfirm --needed \
        ca-certificates \
        curl \
        git \
        unzip \
        xz \
        tar \
        gzip \
        rsync \
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

install_packages() {

    case "$OS_ID" in

        ubuntu|debian|linuxmint|pop|elementary)
            install_apt
            ;;

        arch|omarchy|cachyos|endeavouros|manjaro)
            install_pacman
            ;;

        fedora|rhel|rocky|almalinux|centos)
            if command -v dnf >/dev/null 2>&1; then
                install_dnf
            else
                yum install -y \
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
                    pkgconfig \
                    openssl \
                    openssl-devel \
                    zlib-devel \
                    procps \
                    iproute \
                    iputils \
                    net-tools \
                    lsof \
                    jq
            fi
            ;;

        *)
            warn "Unknown distribution: $OS_ID"

            if command -v apt-get >/dev/null 2>&1; then
                install_apt
            elif command -v pacman >/dev/null 2>&1; then
                install_pacman
            elif command -v dnf >/dev/null 2>&1; then
                install_dnf
            else
                fail "Unsupported Linux distribution."
            fi
            ;;
    esac
}

# ============================================================
# Repository
# ============================================================

download_source() {

    TMP_ROOT="$(mktemp -d /tmp/spiderpanel.XXXXXX)" || \
        fail "Could not create temporary directory."

    log "Downloading SpiderPanel..."

    git clone \
        --depth 1 \
        --branch "$BRANCH" \
        --single-branch \
        "$REPO" \
        "$TMP_ROOT/app" || \
        fail "Could not clone repository."

    [[ -f "$TMP_ROOT/app/main.py" ]] || \
        fail "main.py not found."

    [[ -f "$TMP_ROOT/app/requirements.txt" ]] || \
        fail "requirements.txt not found."

    ok "Repository downloaded."
}

# ============================================================
# Application deployment
# ============================================================

deploy_application() {

    mkdir -p "$APP_DIR"

    local PERSIST="$TMP_ROOT/persist"

    mkdir -p "$PERSIST"

    # Preserve database/data
    if [[ -d "$APP_DIR/data" ]]; then
        cp -a "$APP_DIR/data" "$PERSIST/data"
    fi

    # Preserve app .env
    if [[ -f "$APP_DIR/.env" ]]; then
        cp -f "$APP_DIR/.env" "$PERSIST/.env"
    fi

    # Preserve credentials
    if [[ -f "$CREDENTIALS_FILE" ]]; then
        cp -f "$CREDENTIALS_FILE" "$PERSIST/INSTALL-CREDENTIALS.txt"
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
        "$APP_DIR/" || \
        fail "Could not install application files."

    # Restore data
    if [[ -d "$PERSIST/data" ]]; then
        rm -rf "$APP_DIR/data"
        cp -a "$PERSIST/data" "$APP_DIR/data"
    fi

    # Restore .env
    if [[ -f "$PERSIST/.env" ]]; then
        cp -f "$PERSIST/.env" "$APP_DIR/.env"
    fi

    # Restore credentials
    if [[ -f "$PERSIST/INSTALL-CREDENTIALS.txt" ]]; then
        cp -f \
            "$PERSIST/INSTALL-CREDENTIALS.txt" \
            "$CREDENTIALS_FILE"
    fi

    mkdir -p \
        "$APP_DIR/data" \
        "$APP_DIR/data/scanned" \
        "$APP_DIR/xray"

    chmod 755 "$APP_DIR"

    ok "Application installed."
}

# ============================================================
# UV
#
# IMPORTANT:
# Direct official release download.
# No astral.sh install script.
# ============================================================

install_uv() {

    if [[ -x "$UV_BIN" ]]; then

        if "$UV_BIN" --version >/dev/null 2>&1; then
            ok "uv already installed: $("$UV_BIN" --version)"
            return 0
        fi
    fi

    log "Installing uv ${UV_VERSION}..."

    local ARCH
    local UV_ARCH
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
            fail "Unsupported CPU architecture: $ARCH"
            ;;
    esac

    TMP_UV="$(mktemp -d /tmp/spiderpanel-uv.XXXXXX)"

    ARCHIVE="$TMP_UV/uv.tar.gz"

    URL="https://github.com/astral-sh/uv/releases/download/v${UV_VERSION}/uv-${UV_ARCH}.tar.gz"

    log "Downloading:"
    log "$URL"

    curl -fL \
        --retry 5 \
        --retry-delay 2 \
        --connect-timeout 20 \
        --max-time 300 \
        "$URL" \
        -o "$ARCHIVE" || {

        rm -rf "$TMP_UV"

        fail "Could not download uv."
    }

    log "Extracting uv..."

    tar -xzf "$ARCHIVE" -C "$TMP_UV" || {

        rm -rf "$TMP_UV"

        fail "Could not extract uv."
    }

    UV_SOURCE="$(
        find "$TMP_UV" \
            -type f \
            -name uv \
            -perm -u+x \
            2>/dev/null |
        head -n1 ||
        true
    )"

    UVX_SOURCE="$(
        find "$TMP_UV" \
            -type f \
            -name uvx \
            -perm -u+x \
            2>/dev/null |
        head -n1 ||
        true
    )"

    [[ -n "$UV_SOURCE" ]] || {

        rm -rf "$TMP_UV"

        fail "uv binary not found in archive."
    }

    install -Dm755 "$UV_SOURCE" "$UV_BIN" || {

        rm -rf "$TMP_UV"

        fail "Could not install uv."
    }

    if [[ -n "$UVX_SOURCE" ]]; then
        install -Dm755 "$UVX_SOURCE" "$UVX_BIN" || true
    fi

    rm -rf "$TMP_UV"

    hash -r 2>/dev/null || true

    [[ -x "$UV_BIN" ]] || \
        fail "uv was not installed to $UV_BIN."

    "$UV_BIN" --version >/dev/null 2>&1 || \
        fail "uv exists but cannot execute."

    ok "uv installed successfully: $("$UV_BIN" --version)"
}

# ============================================================
# Python 3.12
# ============================================================

setup_python() {

    log "Preparing dedicated Python 3.12 runtime..."

    install_uv

    log "Installing Python 3.12..."

    "$UV_BIN" python install 3.12 || \
        fail "Could not install Python 3.12."

    local PY312

    PY312="$(
        "$UV_BIN" python find 3.12 2>/dev/null |
        head -n1 ||
        true
    )"

    [[ -n "$PY312" ]] || \
        fail "Python 3.12 could not be found."

    [[ -x "$PY312" ]] || \
        fail "Python 3.12 binary is not executable."

    local PY_VERSION

    PY_VERSION="$(
        "$PY312" -c \
        'import sys; print(".".join(map(str,sys.version_info[:3])))'
    )"

    [[ "$PY_VERSION" == 3.12.* ]] || \
        fail "Wrong Python version detected: $PY_VERSION"

    ok "Python selected: $PY312"
    ok "Python version: $PY_VERSION"

    # --------------------------------------------------------
    # REMOVE OLD 3.14 VENV
    # --------------------------------------------------------

    if [[ -x "$VENV_DIR/bin/python" ]]; then

        local OLD_VERSION

        OLD_VERSION="$(
            "$VENV_DIR/bin/python" \
            -c \
            'import sys; print(".".join(map(str,sys.version_info[:2])))' \
            2>/dev/null ||
            true
        )"

        if [[ "$OLD_VERSION" != "3.12" ]]; then

            warn "Old virtual environment uses Python $OLD_VERSION."
            log "Removing old virtual environment..."

            rm -rf "$VENV_DIR"
        fi
    fi

    # --------------------------------------------------------
    # ALWAYS CREATE WITH PYTHON 3.12
    # --------------------------------------------------------

    rm -rf "$VENV_DIR"

    log "Creating Python 3.12 virtual environment..."

    "$UV_BIN" venv \
        --python "$PY312" \
        "$VENV_DIR" || \
        fail "Could not create Python virtual environment."

    local VENV_PYTHON="$VENV_DIR/bin/python"

    [[ -x "$VENV_PYTHON" ]] || \
        fail "Virtual environment Python missing."

    local FINAL_VERSION

    FINAL_VERSION="$(
        "$VENV_PYTHON" \
        -c \
        'import sys; print(".".join(map(str,sys.version_info[:3])))'
    )"

    [[ "$FINAL_VERSION" == 3.12.* ]] || \
        fail "Virtual environment has wrong Python: $FINAL_VERSION"

    ok "Virtual environment ready: Python $FINAL_VERSION"

    # --------------------------------------------------------
    # Pip tools
    # --------------------------------------------------------

    log "Upgrading packaging tools..."

    "$VENV_PYTHON" -m pip install \
        --upgrade \
        --no-cache-dir \
        pip \
        setuptools \
        wheel || \
        fail "Could not upgrade pip/setuptools/wheel."

    # --------------------------------------------------------
    # Requirements
    # --------------------------------------------------------

    log "Installing SpiderPanel dependencies..."

    "$VENV_PYTHON" -m pip install \
        --no-cache-dir \
        --only-binary=:all: \
        -r "$APP_DIR/requirements.txt" || {

        warn "Binary-only installation failed."
        warn "Retrying normally with Python 3.12..."

        "$VENV_PYTHON" -m pip install \
            --no-cache-dir \
            -r "$APP_DIR/requirements.txt" || \
            fail "Python dependency installation failed."
    }

    # --------------------------------------------------------
    # Dependency test
    # --------------------------------------------------------

    log "Testing Python dependencies..."

    "$VENV_PYTHON" - <<'PY'
import sys

assert sys.version_info[:2] == (3, 12)

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
print("All dependencies: OK")
PY

    ok "Python 3.12 environment is ready."
}

# ============================================================
# Xray
# ============================================================

install_xray() {

    mkdir -p "$XRAY_DIR"

    if [[ -x "$XRAY_BIN" ]] &&
       "$XRAY_BIN" version >/dev/null 2>&1; then

        ok "Xray already installed."
        return 0
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

    log "Installing Xray ${XRAY_VERSION}..."

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

    if ! unzip -o "$ZIP" \
        -d "$XRAY_DIR" >/dev/null; then

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
            warn "Xray validation failed."
        fi
    else
        warn "Xray binary not found."
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

    command -v git >/dev/null 2>&1 || {
        warn "git unavailable; MTProxy skipped."
        return 0
    }

    local TMP_MT
    local MT_BIN

    TMP_MT="$(mktemp -d /tmp/spiderpanel-mtproxy.XXXXXX)"

    log "Downloading MTProxy..."

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

        warn "MTProxy binary not found."
        return 0
    fi

    install -Dm755 \
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
# Credentials
# ============================================================

generate_secret() {

    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
        return
    fi

    od -An -N32 -tx1 /dev/urandom |
        tr -d ' \n'
}

generate_password() {

    generate_secret | head -c 20
}

# ============================================================
# Environment
# ============================================================

setup_environment() {

    log "Configuring environment..."

    mkdir -p "$APP_DIR/data"

    local SECRET_KEY=""
    local ADMIN_PASSWORD=""

    if [[ -f "$ENV_FILE" ]]; then

        SECRET_KEY="$(
            grep '^SECRET_KEY=' "$ENV_FILE" |
            head -n1 |
            cut -d= -f2- ||
            true
        )

        ADMIN_PASSWORD="$(
            grep '^ADMIN_PASSWORD=' "$ENV_FILE" |
            head -n1 |
            cut -d= -f2- ||
            true
        )
    fi

    [[ -n "$SECRET_KEY" ]] || \
        SECRET_KEY="$(generate_secret)"

    [[ -n "$ADMIN_PASSWORD" ]] || \
        ADMIN_PASSWORD="$(generate_password)"

    cat > "$ENV_FILE" <<EOF
SECRET_KEY=$SECRET_KEY
ADMIN_PASSWORD=$ADMIN_PASSWORD

PORT=8080

DATA_DIR=$APP_DIR/data
SPIDER_DATA_DIR=$APP_DIR/data

XRAY_BIN=$XRAY_BIN
MTPROXY_PROXY_BIN=$MTPROXY_BIN
MTPROXY_PROXY_PATH=$MTPROXY_BIN
MTPROTO_PROXY_BIN=$MTPROXY_BIN

WORKER_SYNC_INTERVAL=3600

RAILWAY_PUBLIC_DOMAIN=

PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
PIP_NO_CACHE_DIR=1
EOF

    chmod 600 "$ENV_FILE"

    cat > "$CREDENTIALS_FILE" <<EOF
======================================================
                    SpiderPanel
======================================================

Application:
  http://127.0.0.1:8080/spider

Port:
  8080

Admin Password:
  $ADMIN_PASSWORD

Application Directory:
  $APP_DIR

Environment File:
  $ENV_FILE

Python:
  $VENV_DIR/bin/python

Xray:
  $XRAY_BIN

MTProxy:
  $MTPROXY_BIN

======================================================
EOF

    chmod 600 "$CREDENTIALS_FILE"

    ok "Environment configured."
}

# ============================================================
# Validation
# ============================================================

validate_application() {

    log "Validating application..."

    local PYTHON="$VENV_DIR/bin/python"

    [[ -x "$PYTHON" ]] || \
        fail "Application Python not found."

    "$PYTHON" -m compileall -q "$APP_DIR" || \
        fail "Python compilation check failed."

    "$PYTHON" -c "import fastapi, uvicorn" || \
        fail "FastAPI/Uvicorn import test failed."

    ok "Application validation completed."
}

# ============================================================
# Systemd
# ============================================================

create_systemd_service() {

    [[ "$HAS_SYSTEMD" -eq 1 ]] || return 0

    log "Creating systemd service..."

    cat > "$SERVICE_FILE" <<EOF
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

    ok "systemd service created."
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
        fail "uvicorn not found."

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

    if [[ "$HAS_SYSTEMD" -eq 1 ]]; then

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

    if [[ "$HAS_SYSTEMD" -eq 1 ]]; then

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
    echo "                SpiderPanel Status"
    echo "======================================================"

    echo "OS:        $OS_NAME"
    echo "Version:   $OS_VERSION"

    if [[ "$IS_CODESPACE" -eq 1 ]]; then
        echo "Mode:      GitHub Codespaces"
    elif [[ "$HAS_SYSTEMD" -eq 1 ]]; then
        echo "Mode:      systemd"
    else
        echo "Mode:      standalone"
    fi

    echo "Port:      8080"

    if [[ -x "$VENV_DIR/bin/python" ]]; then
        echo "Python:    $("$VENV_DIR/bin/python" --version 2>&1)"
    else
        echo "Python:    missing"
    fi

    if [[ "$HAS_SYSTEMD" -eq 1 ]]; then

        echo "Service:   $(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo inactive)"

    else

        if is_running_standalone; then
            echo "Process:   running"
            echo "PID:       $(cat "$PID_FILE")"
        else
            echo "Process:   stopped"
        fi
    fi

    echo "======================================================"
    echo
}

# ============================================================
# IP
# ============================================================

get_local_ip() {

    local IP=""

    IP="$(
        hostname -I 2>/dev/null |
        awk '{print $1}' ||
        true
    )"

    if [[ -z "$IP" ]] && command -v ip >/dev/null 2>&1; then

        IP="$(
            ip route get 1.1.1.1 2>/dev/null |
            awk '/src/ {
                for(i=1;i<=NF;i++)
                    if($i=="src") {
                        print $(i+1)
                        exit
                    }
            }' ||
            true
        )"
    fi

    echo "${IP:-127.0.0.1}"
}

get_public_ip() {

    local IP

    IP="$(
        curl -4 -fsSL \
            --connect-timeout 5 \
            --max-time 10 \
            https://api.ipify.org \
            2>/dev/null ||
        true
    )"

    if [[ -z "$IP" ]]; then

        IP="$(
            curl -4 -fsSL \
                --connect-timeout 5 \
                --max-time 10 \
                https://ifconfig.me \
                2>/dev/null ||
            true
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

    echo "https://${CODESPACE_NAME}-8080.${DOMAIN}/spider"
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
            grep '^ADMIN_PASSWORD=' "$ENV_FILE" |
            head -n1 |
            cut -d= -f2- ||
            true
        )
    fi

    LOCAL_IP="$(get_local_ip)"
    PUBLIC_IP="$(get_public_ip)"

    echo
    echo "======================================================"
    echo "                    SPIDERPANEL"
    echo "======================================================"
    echo

    if [[ "$IS_CODESPACE" -eq 1 ]]; then
        echo "Mode:"
        echo "  GitHub Codespaces / standalone"
    elif [[ "$HAS_SYSTEMD" -eq 1 ]]; then
        echo "Mode:"
        echo "  VPS / systemd"
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

    if [[ "$IS_CODESPACE" -eq 1 ]]; then

        echo
        echo "Codespace URL:"
        echo "  $(get_codespace_url)"

        echo
        echo "Codespaces:"
        echo "  Forward port 8080 from the Ports tab."
    fi

    echo
    echo "Admin Password:"
    echo "  ${PASSWORD:-Unavailable}"

    echo
    echo "Credentials:"
    echo "  $CREDENTIALS_FILE"

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

    if [[ "$HAS_SYSTEMD" -eq 1 ]]; then

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

    local UPDATE_SCRIPT

    UPDATE_SCRIPT="$(mktemp /tmp/spiderpanel-update.XXXXXX.sh)"

    curl -fsSL \
        --retry 5 \
        --retry-delay 2 \
        --connect-timeout 15 \
        --max-time 300 \
        "$INSTALLER_URL" \
        -o "$UPDATE_SCRIPT" || {

        rm -f "$UPDATE_SCRIPT"

        fail "Could not download update installer."
    }

    chmod 700 "$UPDATE_SCRIPT"

    bash "$UPDATE_SCRIPT" install

    rm -f "$UPDATE_SCRIPT"
}

# ============================================================
# Uninstall
# ============================================================

uninstall_panel() {

    echo
    echo "======================================================"
    echo "WARNING: SpiderPanel will be removed."
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
        echo "Cancelled."
        return 0
    fi

    if [[ "$HAS_SYSTEMD" -eq 1 ]]; then

        systemctl disable \
            --now \
            "$SERVICE_NAME" \
            >/dev/null 2>&1 || true

        rm -f "$SERVICE_FILE"

        systemctl daemon-reload || true

    else

        stop_standalone || true
    fi

    rm -f "$CLI_PATH"
    rm -f "$ENV_FILE"

    rm -rf "$APP_DIR"

    ok "SpiderPanel removed."
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

case "${1:-menu}" in

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

    logs)
        exec bash "$APP_DIR/start.sh" logs
        ;;

    install)
        exec bash "$APP_DIR/start.sh" install
        ;;

    update)

        TMP_UPDATE="$(mktemp /tmp/spiderpanel-cli-update.XXXXXX.sh)"

        curl -fsSL \
            --retry 5 \
            --retry-delay 2 \
            "$INSTALLER_URL" \
            -o "$TMP_UPDATE"

        chmod 700 "$TMP_UPDATE"

        sudo bash "$TMP_UPDATE" install

        rm -f "$TMP_UPDATE"
        ;;

    uninstall)
        exec bash "$APP_DIR/start.sh" uninstall
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

    chmod 755 "$CLI_PATH"

    ok "Global command installed: spiderpanel"
}

# ============================================================
# Installation
# ============================================================

install_panel() {

    detect_os
    detect_environment

    echo
    echo "======================================================"
    echo "              SpiderPanel Installer"
    echo "======================================================"
    echo
    echo "OS:          $OS_NAME"
    echo "Version:     $OS_VERSION"

    if [[ "$IS_CODESPACE" -eq 1 ]]; then
        echo "Environment: GitHub Codespaces"
    elif [[ "$HAS_SYSTEMD" -eq 1 ]]; then
        echo "Environment: Linux + systemd"
    else
        echo "Environment: Linux + standalone"
    fi

    echo "Port:        8080"
    echo
    echo "======================================================"
    echo

    install_packages

    download_source

    deploy_application

    setup_python

    install_xray || true

    install_mtproxy || true

    setup_environment

    validate_application

    create_systemd_service

    # Store exact installer inside app
    cp -f \
        "$TMP_ROOT/app/start.sh" \
        "$APP_DIR/start.sh" \
        2>/dev/null || true

    chmod 755 "$APP_DIR/start.sh" 2>/dev/null || true

    create_cli

    start_panel

    info_panel

    ok "SpiderPanel installation completed successfully."
}

# ============================================================
# Main
# ============================================================

main() {

    case "${1:-install}" in

        install)
            become_root "$@"
            install_panel
            ;;

        info)
            become_root "$@"
            detect_os
            detect_environment
            info_panel
            ;;

        status)
            become_root "$@"
            detect_os
            detect_environment
            status_panel
            ;;

        start)
            become_root "$@"
            detect_os
            detect_environment
            start_panel
            ;;

        stop)
            become_root "$@"
            detect_os
            detect_environment
            stop_panel
            ;;

        restart)
            become_root "$@"
            detect_os
            detect_environment
            restart_panel
            ;;

        update)
            become_root "$@"
            update_panel
            ;;

        logs)
            become_root "$@"
            detect_os
            detect_environment
            logs_panel
            ;;

        uninstall)
            become_root "$@"
            detect_os
            detect_environment
            uninstall_panel
            ;;

        *)
            echo
            echo "SpiderPanel"
            echo
            echo "Usage:"
            echo
            echo "  start.sh install"
            echo "  start.sh info"
            echo "  start.sh status"
            echo "  start.sh start"
            echo "  start.sh stop"
            echo "  start.sh restart"
            echo "  start.sh update"
            echo "  start.sh logs"
            echo "  start.sh uninstall"
            echo
            ;;
    esac
}

main "$@"
