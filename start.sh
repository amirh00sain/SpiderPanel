#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# SpiderPanel Universal Installer / Manager
# ============================================================

APP_DIR="${SPIDER_APP_DIR:-/opt/SpiderPanel}"
REPO="${SPIDER_REPO:-https://github.com/amirh00sain/SpiderPanel.git}"
BRANCH="${SPIDER_BRANCH:-main}"

INSTALLER_URL="${SPIDER_INSTALLER_URL:-https://raw.githubusercontent.com/amirh00sain/SpiderPanel/main/start.sh}"

ENV_FILE="/etc/spider-panel.env"
SERVICE="spider-panel"

PORT="8080"

VENV="$APP_DIR/.venv"
PIDFILE="$APP_DIR/spiderpanel.pid"
LOGFILE="$APP_DIR/spiderpanel.log"

CLI="/usr/local/bin/spiderpanel"

XRAY="$APP_DIR/xray/xray"
MTPROXY="/usr/local/bin/mtproto-proxy"

UV="/usr/local/bin/uv"

# Pin uv to a known release.
UV_VERSION="${SPIDER_UV_VERSION:-0.12.9}"

# Xray version.
XRAY_VERSION="${SPIDER_XRAY_VERSION:-26.3.27}"

TMP_ROOT=""

OS_ID=""
OS_NAME=""
PID1=""
HAS_SYSTEMD=0
IS_CODESPACE=0


# ============================================================
# OUTPUT
# ============================================================

log() {
    printf '[SpiderPanel] %s\n' "$*"
}

ok() {
    printf '[OK] %s\n' "$*"
}

warn() {
    printf '[WARN] %s\n' "$*" >&2
}

fail() {
    printf '[ERROR] %s\n' "$*" >&2
    exit 1
}


# ============================================================
# CLEANUP
# ============================================================

cleanup() {
    if [[ -n "${TMP_ROOT:-}" ]]; then
        rm -rf "$TMP_ROOT" 2>/dev/null || true
    fi
}

trap cleanup EXIT


# ============================================================
# ROOT
# ============================================================

root() {

    if (( EUID == 0 )); then
        return 0
    fi

    command -v sudo >/dev/null 2>&1 \
        || fail "sudo is required."

    local f

    f="$(mktemp /tmp/spiderpanel-root.XXXXXX)"

    curl -fsSL \
        --retry 5 \
        --retry-delay 2 \
        "$INSTALLER_URL" \
        -o "$f" \
        || fail "Cannot download installer."

    chmod 700 "$f"

    exec sudo -E bash "$f" "$@"
}


# ============================================================
# OS DETECTION
# ============================================================

detect() {

    [[ -r /etc/os-release ]] \
        || fail "Cannot detect operating system."

    . /etc/os-release

    OS_ID="${ID:-unknown}"
    OS_NAME="${PRETTY_NAME:-$ID}"

    PID1="$(
        ps -p 1 -o comm= 2>/dev/null \
        | tr -d '[:space:]' \
        || true
    )"

    if [[ "$PID1" == "systemd" ]]; then
        HAS_SYSTEMD=1
    else
        HAS_SYSTEMD=0
    fi

    if [[ "${CODESPACES:-}" == "true" || -n "${CODESPACE_NAME:-}" ]]; then
        IS_CODESPACE=1
    else
        IS_CODESPACE=0
    fi
}


# ============================================================
# SYSTEMD
# ============================================================

systemd_ok() {
    [[ "$HAS_SYSTEMD" == "1" ]]
}


# ============================================================
# PACKAGE INSTALL
# ============================================================

packages() {

    log "Installing system dependencies..."

    case "$OS_ID" in

        arch|omarchy|cachyos|endeavouros|manjaro)

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
                jq \
                python \
                python-pip \
                python-virtualenv \
                || fail "System package installation failed."

            ;;

        ubuntu|debian|linuxmint|pop)

            export DEBIAN_FRONTEND=noninteractive

            apt-get update -y \
                || fail "apt update failed."

            apt-get install -y \
                ca-certificates \
                curl \
                git \
                unzip \
                xz-utils \
                tar \
                gzip \
                rsync \
                build-essential \
                openssl \
                libssl-dev \
                zlib1g-dev \
                procps \
                iproute2 \
                iputils-ping \
                net-tools \
                lsof \
                jq \
                python3 \
                python3-pip \
                python3-venv \
                || fail "System package installation failed."

            ;;

        fedora|rhel|rocky|almalinux|centos)

            if command -v dnf >/dev/null 2>&1; then

                dnf install -y \
                    ca-certificates \
                    curl \
                    git \
                    unzip \
                    xz \
                    tar \
                    gzip \
                    rsync \
                    gcc \
                    gcc-c++ \
                    make \
                    openssl \
                    openssl-devel \
                    zlib-devel \
                    procps \
                    iproute \
                    iputils \
                    net-tools \
                    lsof \
                    jq \
                    python3 \
                    python3-pip \
                    || fail "System package installation failed."

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
                    gcc \
                    gcc-c++ \
                    make \
                    openssl \
                    openssl-devel \
                    zlib-devel \
                    procps \
                    iproute \
                    iputils \
                    net-tools \
                    lsof \
                    jq \
                    python3 \
                    python3-pip \
                    || fail "System package installation failed."

            fi

            ;;

        *)

            if command -v pacman >/dev/null 2>&1; then

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
                    jq \
                    python \
                    python-pip \
                    python-virtualenv \
                    || fail "System package installation failed."

            elif command -v apt-get >/dev/null 2>&1; then

                export DEBIAN_FRONTEND=noninteractive

                apt-get update -y \
                    || fail "apt update failed."

                apt-get install -y \
                    ca-certificates \
                    curl \
                    git \
                    unzip \
                    xz-utils \
                    tar \
                    gzip \
                    rsync \
                    build-essential \
                    openssl \
                    libssl-dev \
                    zlib1g-dev \
                    procps \
                    iproute2 \
                    iputils-ping \
                    net-tools \
                    lsof \
                    jq \
                    python3 \
                    python3-pip \
                    python3-venv \
                    || fail "System package installation failed."

            elif command -v dnf >/dev/null 2>&1; then

                dnf install -y \
                    ca-certificates \
                    curl \
                    git \
                    unzip \
                    xz \
                    tar \
                    gzip \
                    rsync \
                    gcc \
                    gcc-c++ \
                    make \
                    openssl \
                    openssl-devel \
                    zlib-devel \
                    procps \
                    iproute \
                    iputils \
                    net-tools \
                    lsof \
                    jq \
                    python3 \
                    python3-pip \
                    || fail "System package installation failed."

            else

                fail "Unsupported operating system: $OS_ID"

            fi

            ;;

    esac

    ok "System dependencies installed."
}


# ============================================================
# DOWNLOAD REPOSITORY
# ============================================================

download_repo() {

    TMP_ROOT="$(mktemp -d /tmp/spiderpanel.XXXXXX)"

    log "Downloading SpiderPanel..."

    git clone \
        --depth 1 \
        --branch "$BRANCH" \
        --single-branch \
        "$REPO" \
        "$TMP_ROOT/app" \
        || fail "Git clone failed."

    [[ -f "$TMP_ROOT/app/main.py" ]] \
        || fail "main.py not found in repository."

    [[ -f "$TMP_ROOT/app/requirements.txt" ]] \
        || fail "requirements.txt not found in repository."

    ok "Repository downloaded."
}


# ============================================================
# DEPLOY
# ============================================================

deploy() {

    mkdir -p "$APP_DIR"

    local keep="$TMP_ROOT/keep"

    mkdir -p "$keep"

    if [[ -d "$APP_DIR/data" ]]; then
        cp -a "$APP_DIR/data" "$keep/data"
    fi

    if [[ -f "$APP_DIR/.env" ]]; then
        cp -f "$APP_DIR/.env" "$keep/.env"
    fi

    log "Installing application files..."

    rsync -a \
        --delete \
        --exclude '.git/' \
        --exclude '.venv/' \
        --exclude 'data/' \
        --exclude '*.pid' \
        --exclude '*.log' \
        "$TMP_ROOT/app/" \
        "$APP_DIR/" \
        || fail "Application deployment failed."

    if [[ -d "$keep/data" ]]; then
        rm -rf "$APP_DIR/data"
        cp -a "$keep/data" "$APP_DIR/data"
    fi

    if [[ -f "$keep/.env" ]]; then
        cp -f "$keep/.env" "$APP_DIR/.env"
    fi

    mkdir -p \
        "$APP_DIR/data" \
        "$APP_DIR/xray"

    chmod 755 "$APP_DIR"

    ok "Application installed."
}


# ============================================================
# UV ARCH DETECTION
# ============================================================

detect_uv_arch() {

    case "$(uname -m)" in

        x86_64|amd64)
            echo "x86_64"
            ;;

        aarch64|arm64)
            echo "aarch64"
            ;;

        armv7l)
            echo "armv7"
            ;;

        *)
            fail "Unsupported CPU architecture for uv: $(uname -m)"
            ;;

    esac
}


# ============================================================
# INSTALL UV
#
# IMPORTANT:
# We intentionally DO NOT use astral.sh/install.sh here.
#
# We download the official uv release archive directly.
#
# This completely avoids the:
#
#   installing to /usr/local
#   uv was not found
#
# problem.
# ============================================================

install_uv() {

    local arch=""
    local uv_target="$UV"
    local tmp=""
    local archive=""
    local url=""

    export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"


    # --------------------------------------------------------
    # Existing working uv
    # --------------------------------------------------------

    if [[ -x "$uv_target" ]] \
        && "$uv_target" --version >/dev/null 2>&1
    then

        ok "uv ready: $("$uv_target" --version)"

        return 0
    fi


    # --------------------------------------------------------
    # Search system
    # --------------------------------------------------------

    local found=""

    for candidate in \
        /usr/local/bin/uv \
        /usr/bin/uv \
        /root/.local/bin/uv \
        /root/.cargo/bin/uv \
        /usr/local/uv/uv
    do

        if [[ -x "$candidate" ]] \
            && "$candidate" --version >/dev/null 2>&1
        then

            found="$candidate"

            break
        fi

    done


    if [[ -n "$found" ]]; then

        if [[ "$found" != "$uv_target" ]]; then

            ln -sf "$found" "$uv_target"

        fi

        ok "uv ready: $("$uv_target" --version)"

        return 0
    fi


    # --------------------------------------------------------
    # Determine architecture
    # --------------------------------------------------------

    arch="$(detect_uv_arch)"


    case "$arch" in

        x86_64)
            archive="uv-x86_64-unknown-linux-gnu.tar.gz"
            ;;

        aarch64)
            archive="uv-aarch64-unknown-linux-gnu.tar.gz"
            ;;

        armv7)
            archive="uv-armv7-unknown-linux-gnueabihf.tar.gz"
            ;;

        *)
            fail "Unsupported uv architecture: $arch"

            ;;

    esac


    url="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${archive}"


    log "Installing uv ${UV_VERSION}..."

    log "Architecture: $arch"

    log "Downloading official uv binary..."


    tmp="$(mktemp -d /tmp/spiderpanel-uv.XXXXXX)"

    archive_path="$tmp/$archive"


    curl \
        -fL \
        --retry 5 \
        --retry-delay 2 \
        --connect-timeout 15 \
        --max-time 300 \
        "$url" \
        -o "$archive_path" \
        || {
            rm -rf "$tmp"
            fail "uv download failed."
        }


    [[ -s "$archive_path" ]] \
        || {
            rm -rf "$tmp"
            fail "Downloaded uv archive is empty."
        }


    # --------------------------------------------------------
    # Extract
    # --------------------------------------------------------

    tar \
        -xzf "$archive_path" \
        -C "$tmp" \
        || {
            rm -rf "$tmp"
            fail "uv archive extraction failed."
        }


    local binary=""

    binary="$(
        find "$tmp" \
            -type f \
            -name uv \
            -perm -u+x \
            -print \
            -quit
    )"


    [[ -n "$binary" ]] \
        || {
            rm -rf "$tmp"
            fail "uv binary was not found inside archive."
        }


    # --------------------------------------------------------
    # Install
    # --------------------------------------------------------

    install \
        -Dm755 \
        "$binary" \
        "$uv_target" \
        || {
            rm -rf "$tmp"
            fail "Cannot install uv to $uv_target."
        }


    rm -rf "$tmp"


    # --------------------------------------------------------
    # Verify
    # --------------------------------------------------------

    [[ -x "$uv_target" ]] \
        || fail "uv installation completed but binary is missing."


    "$uv_target" --version >/dev/null 2>&1 \
        || fail "uv binary exists but cannot execute."


    local installed_version=""

    installed_version="$(
        "$uv_target" --version 2>/dev/null
    )"


    ok "uv ready: $installed_version"
}


# ============================================================
# PYTHON 3.12
# ============================================================

setup_python() {

    install_uv

    export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

    log "Preparing dedicated Python 3.12 runtime..."


    # --------------------------------------------------------
    # uv Python
    # --------------------------------------------------------

    "$UV" python install 3.12 \
        || fail "Python 3.12 installation failed."


    # --------------------------------------------------------
    # Find Python
    # --------------------------------------------------------

    local py=""

    py="$(
        "$UV" python find 3.12 2>/dev/null \
        | head -n1 \
        || true
    )"


    [[ -n "$py" ]] \
        || fail "Python 3.12 binary not found."


    [[ -x "$py" ]] \
        || fail "Python 3.12 binary is not executable."


    # --------------------------------------------------------
    # Verify
    # --------------------------------------------------------

    local version=""

    version="$(
        "$py" -c \
        'import sys; print(".".join(map(str, sys.version_info[:2])))'
    )"


    [[ "$version" == "3.12" ]] \
        || fail "Wrong Python selected: $version"


    # --------------------------------------------------------
    # venv
    # --------------------------------------------------------

    rm -rf "$VENV"

    log "Creating Python 3.12 virtual environment..."


    "$UV" venv \
        --python "$py" \
        "$VENV" \
        || fail "Virtual environment creation failed."


    local p="$VENV/bin/python"


    [[ -x "$p" ]] \
        || fail "venv Python missing."


    version="$(
        "$p" -c \
        'import sys; print(".".join(map(str, sys.version_info[:2])))'
    )"


    [[ "$version" == "3.12" ]] \
        || fail "venv is not Python 3.12."


    # --------------------------------------------------------
    # Pip
    # --------------------------------------------------------

    log "Preparing pip..."


    "$p" -m ensurepip \
        --upgrade \
        >/dev/null 2>&1 \
        || true


    "$p" -m pip install \
        --upgrade \
        --no-cache-dir \
        pip \
        setuptools \
        wheel \
        || fail "Packaging tools installation failed."


    # --------------------------------------------------------
    # Dependencies
    # --------------------------------------------------------

    log "Installing Python dependencies..."


    if ! "$p" -m pip install \
        --no-cache-dir \
        --only-binary=:all: \
        -r "$APP_DIR/requirements.txt"
    then

        warn "Binary-only dependency installation failed."

        log "Retrying with source packages..."


        "$p" -m pip install \
            --no-cache-dir \
            -r "$APP_DIR/requirements.txt" \
            || fail "Dependency installation failed."

    fi


    # --------------------------------------------------------
    # Test
    # --------------------------------------------------------

    "$p" - <<'PY'
import sys

required = [
    "fastapi",
    "uvicorn",
    "httpx",
    "websockets",
    "aiofiles",
    "qrcode",
    "PIL",
    "psutil",
    "cryptography",
    "socks",
]

assert sys.version_info[:2] == (3, 12), sys.version

for module in required:
    __import__(module)

print("Python dependency test: OK")
PY


    ok "Python runtime ready: $("$p" --version)"
}


# ============================================================
# PASSWORD / SECRET
# ============================================================

generate_secret() {

    openssl rand -hex 32
}


generate_password() {

    openssl rand -hex 10
}


# ============================================================
# CONFIG
# ============================================================

config() {

    mkdir -p "$APP_DIR/data"

    local secret=""
    local password=""


    if [[ -f "$ENV_FILE" ]]; then

        secret="$(
            grep '^SECRET_KEY=' "$ENV_FILE" \
            | head -n1 \
            | cut -d= -f2- \
            || true
        )"


        password="$(
            grep '^ADMIN_PASSWORD=' "$ENV_FILE" \
            | head -n1 \
            | cut -d= -f2- \
            || true
        )"

    fi


    [[ -n "$secret" ]] \
        || secret="$(generate_secret)"


    [[ -n "$password" ]] \
        || password="$(generate_password)"


    cat > "$ENV_FILE" <<EOF
SECRET_KEY=$secret
ADMIN_PASSWORD=$password
PORT=8080
HOST=0.0.0.0
DATA_DIR=$APP_DIR/data
SPIDER_DATA_DIR=$APP_DIR/data
XRAY_BIN=$XRAY
MTPROTO_PROXY_BIN=$MTPROXY
WORKER_SYNC_INTERVAL=3600
RAILWAY_PUBLIC_DOMAIN=
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
PIP_NO_CACHE_DIR=1
EOF


    chmod 600 "$ENV_FILE"


    cat > "$APP_DIR/INSTALL-CREDENTIALS.txt" <<EOF
SpiderPanel
===========

URL:
http://127.0.0.1:8080/spider

Port:
8080

Admin Password:
$password

Application:
$APP_DIR

Environment:
$ENV_FILE

Python:
$VENV/bin/python

Xray:
$XRAY

MTProto:
$MTPROXY
EOF


    chmod 600 "$APP_DIR/INSTALL-CREDENTIALS.txt"


    ok "Configuration completed."
}


# ============================================================
# XRAY
# ============================================================

install_xray() {

    local arch=""
    local xray_arch=""
    local url=""
    local zip=""

    mkdir -p "$APP_DIR/xray"


    if [[ -x "$XRAY" ]] \
        && "$XRAY" version >/dev/null 2>&1
    then

        ok "Xray already installed."

        return 0
    fi


    arch="$(uname -m)"


    case "$arch" in

        x86_64|amd64)
            xray_arch="64"
            ;;

        aarch64|arm64)
            xray_arch="arm64-v8a"
            ;;

        armv7l|armv7)
            xray_arch="arm32-v7a"
            ;;

        i686|i386)
            xray_arch="32"
            ;;

        *)
            warn "Unsupported Xray architecture: $arch"
            return 0
            ;;

    esac


    url="https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-linux-${xray_arch}.zip"

    zip="$APP_DIR/xray.zip"


    log "Installing Xray ${XRAY_VERSION}..."


    if curl \
        -fL \
        --retry 5 \
        --retry-delay 2 \
        "$url" \
        -o "$zip"
    then

        rm -rf "$APP_DIR/xray"

        mkdir -p "$APP_DIR/xray"


        if unzip -o \
            "$zip" \
            -d "$APP_DIR/xray" \
            >/dev/null
        then

            rm -f "$zip"

            chmod +x "$XRAY" \
                2>/dev/null \
                || true


            if [[ -x "$XRAY" ]] \
                && "$XRAY" version >/dev/null 2>&1
            then

                ok "Xray installed."

            else

                warn "Xray extracted but verification failed."

            fi

        else

            rm -f "$zip"

            warn "Xray extraction failed."

        fi

    else

        rm -f "$zip"

        warn "Xray download failed."

    fi
}


# ============================================================
# MTPROXY
# ============================================================

install_mtproxy() {

    if [[ -x "$MTPROXY" ]]; then

        ok "MTProxy already installed."

        return 0
    fi


    local t=""
    local b=""


    t="$(mktemp -d /tmp/mtproxy.XXXXXX)"


    log "Building MTProxy..."


    if ! git clone \
        --depth 1 \
        https://github.com/TelegramMessenger/MTProxy.git \
        "$t/MTProxy"
    then

        rm -rf "$t"

        warn "MTProxy download failed."

        return 0
    fi


    if ! make \
        -C "$t/MTProxy" \
        -j"$(nproc 2>/dev/null || echo 2)"
    then

        rm -rf "$t"

        warn "MTProxy build failed."

        return 0
    fi


    if [[ -x "$t/MTProxy/objs/bin/mtproto-proxy" ]]; then

        b="$t/MTProxy/objs/bin/mtproto-proxy"

    elif [[ -x "$t/MTProxy/mtproto-proxy" ]]; then

        b="$t/MTProxy/mtproto-proxy"

    fi


    if [[ -z "$b" ]]; then

        rm -rf "$t"

        warn "MTProxy binary not found."

        return 0
    fi


    install \
        -Dm755 \
        "$b" \
        "$MTPROXY"


    rm -rf "$t"


    if [[ -x "$MTPROXY" ]]; then

        ok "MTProxy installed."

    else

        warn "MTProxy installation failed."

    fi
}


# ============================================================
# SYSTEMD SERVICE
# ============================================================

create_service() {

    systemd_ok || return 0


    cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=SpiderPanel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$VENV/bin/uvicorn main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF


    systemctl daemon-reload

    systemctl enable \
        "$SERVICE.service" \
        >/dev/null 2>&1 \
        || true
}


# ============================================================
# RUNNING
# ============================================================

running() {

    if systemd_ok; then

        systemctl is-active \
            --quiet \
            "$SERVICE.service"

        return $?
    fi


    if [[ ! -f "$PIDFILE" ]]; then
        return 1
    fi


    local p=""

    p="$(cat "$PIDFILE" 2>/dev/null || echo 0)"


    if [[ "$p" =~ ^[0-9]+$ ]] \
        && kill -0 "$p" 2>/dev/null
    then

        return 0

    fi


    return 1
}


# ============================================================
# START
# ============================================================

start_panel() {

    if systemd_ok; then

        create_service

        systemctl restart "$SERVICE.service"

    else

        if running; then

            ok "SpiderPanel already running."

            return 0
        fi


        touch "$LOGFILE"

        cd "$APP_DIR"


        nohup \
            "$VENV/bin/uvicorn" \
            main:app \
            --host 0.0.0.0 \
            --port 8080 \
            >> "$LOGFILE" 2>&1 &


        echo $! > "$PIDFILE"

    fi


    sleep 3


    if running; then

        ok "SpiderPanel is running."

        return 0
    fi


    printf '[ERROR] SpiderPanel failed to start.\n' >&2


    if systemd_ok; then

        journalctl \
            -u "$SERVICE.service" \
            -n 80 \
            --no-pager \
            || true

    else

        tail \
            -n 80 \
            "$LOGFILE" \
            2>/dev/null \
            || true

    fi


    return 1
}


# ============================================================
# STOP
# ============================================================

stop_panel() {

    if systemd_ok; then

        systemctl stop \
            "$SERVICE.service" \
            >/dev/null 2>&1 \
            || true

    else

        if [[ -f "$PIDFILE" ]]; then

            local p=""

            p="$(cat "$PIDFILE" 2>/dev/null || echo 0)"


            if [[ "$p" =~ ^[0-9]+$ ]]; then

                kill "$p" \
                    2>/dev/null \
                    || true

                sleep 1

                kill -9 "$p" \
                    2>/dev/null \
                    || true

            fi


            rm -f "$PIDFILE"

        fi

    fi


    ok "SpiderPanel stopped."
}


# ============================================================
# STATUS
# ============================================================

status_panel() {

    echo

    echo "SpiderPanel status"
    echo "------------------"

    echo "OS: $OS_NAME"

    echo "Port: 8080"


    if [[ -x "$VENV/bin/python" ]]; then

        echo "Python: $(
            "$VENV/bin/python" --version 2>&1
        )"

    else

        echo "Python: missing"

    fi


    if systemd_ok; then
        echo "Mode: systemd"
    else
        echo "Mode: standalone"
    fi


    if running; then
        echo "Status: RUNNING"
    else
        echo "Status: STOPPED"
    fi


    if [[ -x "$UV" ]] \
        && "$UV" --version >/dev/null 2>&1
    then

        echo "uv: $("$UV" --version)"

    else

        echo "uv: missing"

    fi


    if [[ -x "$XRAY" ]]; then
        echo "Xray: installed"
    else
        echo "Xray: missing"
    fi


    if [[ -x "$MTPROXY" ]]; then
        echo "MTProto: installed"
    else
        echo "MTProto: missing"
    fi


    echo
}


# ============================================================
# IP
# ============================================================

get_public_ip() {

    curl \
        -4 \
        -fsS \
        --max-time 5 \
        https://api.ipify.org \
        2>/dev/null \
        || true
}


get_local_ip() {

    hostname -I \
        2>/dev/null \
        | awk '{print $1}' \
        || true
}


# ============================================================
# INFO
# ============================================================

info_panel() {

    local password=""
    local ip=""
    local local_ip=""


    password="$(
        grep '^ADMIN_PASSWORD=' "$ENV_FILE" \
        2>/dev/null \
        | head -n1 \
        | cut -d= -f2- \
        || true
    )"


    ip="$(get_public_ip)"

    local_ip="$(get_local_ip)"


    echo

    echo "================================================"
    echo "                 SPIDERPANEL"
    echo "================================================"


    echo "URL: http://127.0.0.1:8080/spider"


    if [[ -n "$local_ip" ]]; then

        echo "Local URL: http://${local_ip}:8080/spider"

    fi


    if [[ -n "$ip" ]]; then

        echo "Public URL: http://${ip}:8080/spider"

    fi


    if [[ "$IS_CODESPACE" == "1" ]] \
        && [[ -n "${CODESPACE_NAME:-}" ]]
    then

        local domain="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"

        echo "Codespace URL: https://${CODESPACE_NAME}-8080.${domain}/spider"

        echo "Forward port 8080 in Codespaces."

    fi


    echo

    echo "Admin Password: ${password:-NOT FOUND}"

    echo "Application: $APP_DIR"

    echo "Environment: $ENV_FILE"

    echo "Python: $VENV/bin/python"

    echo "uv: $UV"

    echo "Xray: $XRAY"

    echo "MTProto: $MTPROXY"

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

    echo "================================================"

    echo
}


# ============================================================
# LOGS
# ============================================================

logs_panel() {

    if systemd_ok; then

        journalctl \
            -u "$SERVICE.service" \
            -f \
            --no-pager

    else

        touch "$LOGFILE"

        tail -f "$LOGFILE"

    fi
}


# ============================================================
# UPDATE
# ============================================================

update_panel() {

    local f=""

    f="$(mktemp /tmp/spiderpanel-update.XXXXXX)"


    curl \
        -fsSL \
        --retry 5 \
        --retry-delay 2 \
        "$INSTALLER_URL" \
        -o "$f" \
        || fail "Update download failed."


    chmod 700 "$f"


    bash "$f" install


    rm -f "$f"
}


# ============================================================
# UNINSTALL
# ============================================================

uninstall_panel() {

    echo

    echo "This will remove SpiderPanel."

    echo


    read \
        -r \
        -p "Type REMOVE to continue: " \
        confirmation


    if [[ "$confirmation" != "REMOVE" ]]; then

        echo "Cancelled."

        return 0
    fi


    stop_panel || true


    if systemd_ok; then

        systemctl disable \
            "$SERVICE.service" \
            >/dev/null 2>&1 \
            || true


        rm -f \
            "/etc/systemd/system/$SERVICE.service"


        systemctl daemon-reload \
            >/dev/null 2>&1 \
            || true

    fi


    rm -f "$CLI"

    rm -f "$ENV_FILE"

    rm -rf "$APP_DIR"


    ok "SpiderPanel removed."
}


# ============================================================
# CLI
# ============================================================

create_cli() {

    cat > "$CLI" <<'EOF'
#!/usr/bin/env bash

set -e

APP="/opt/SpiderPanel"

if [[ ! -f "$APP/start.sh" ]]; then
    echo "SpiderPanel is not installed."
    exit 1
fi

case "${1:-menu}" in

    install)
        bash "$APP/start.sh" install
        ;;

    info)
        bash "$APP/start.sh" info
        ;;

    status)
        bash "$APP/start.sh" status
        ;;

    start)
        bash "$APP/start.sh" start
        ;;

    stop)
        bash "$APP/start.sh" stop
        ;;

    restart)
        bash "$APP/start.sh" restart
        ;;

    update)
        bash "$APP/start.sh" update
        ;;

    logs)
        bash "$APP/start.sh" logs
        ;;

    uninstall)
        bash "$APP/start.sh" uninstall
        ;;

    *)
        echo
        echo "SpiderPanel"
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

        read -r -p "Select: " n

        case "$n" in

            1)
                bash "$APP/start.sh" info
                ;;

            2)
                bash "$APP/start.sh" status
                ;;

            3)
                bash "$APP/start.sh" start
                ;;

            4)
                bash "$APP/start.sh" stop
                ;;

            5)
                bash "$APP/start.sh" restart
                ;;

            6)
                bash "$APP/start.sh" update
                ;;

            7)
                bash "$APP/start.sh" logs
                ;;

            8)
                bash "$APP/start.sh" uninstall
                ;;

            0)
                exit 0
                ;;

            *)
                echo "Invalid option."
                ;;

        esac

        ;;

esac
EOF


    chmod 755 "$CLI"

    ok "Global command installed: spiderpanel"
}


# ============================================================
# INSTALL
# ============================================================

install_panel() {

    root "$@"

    detect


    echo

    log "Installing SpiderPanel..."

    log "OS: $OS_NAME"

    log "Port: 8080"


    packages

    download_repo

    deploy

    setup_python

    install_xray || true

    install_mtproxy || true

    config


    log "Checking Python source files..."


    "$VENV/bin/python" \
        -m compileall \
        -q \
        "$APP_DIR" \
        || fail "Python compile check failed."


    # --------------------------------------------------------
    # Preserve installer
    # --------------------------------------------------------

    if [[ -f "$TMP_ROOT/app/start.sh" ]]; then

        cp -f \
            "$TMP_ROOT/app/start.sh" \
            "$APP_DIR/start.sh"

        chmod 755 "$APP_DIR/start.sh"

    fi


    create_cli

    create_service

    start_panel

    info_panel


    ok "Installation completed."
}


# ============================================================
# MAIN
# ============================================================

main() {

    case "${1:-install}" in

        install)

            install_panel "$@"

            ;;

        info)

            root "$@"

            detect

            info_panel

            ;;

        status)

            root "$@"

            detect

            status_panel

            ;;

        start)

            root "$@"

            detect

            start_panel

            ;;

        stop)

            root "$@"

            detect

            stop_panel

            ;;

        restart)

            root "$@"

            detect

            stop_panel

            sleep 1

            start_panel

            ;;

        update)

            root "$@"

            update_panel

            ;;

        logs)

            root "$@"

            detect

            logs_panel

            ;;

        uninstall)

            root "$@"

            detect

            uninstall_panel

            ;;

        *)

            echo
            echo "SpiderPanel Universal Installer"
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

            exit 1

            ;;

    esac
}


main "$@"
