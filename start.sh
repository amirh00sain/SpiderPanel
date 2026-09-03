#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# SpiderPanel Universal Installer / Manager
# ============================================================
#
# INSTALL:
#   curl -fsSL https://raw.githubusercontent.com/amirh00sain/SpiderPanel/main/start.sh | bash
#
# AFTER INSTALL:
#   spiderpanel
#
# COMMANDS:
#   spiderpanel
#   spiderpanel install
#   spiderpanel info
#   spiderpanel status
#   spiderpanel start
#   spiderpanel stop
#   spiderpanel restart
#   spiderpanel update
#   spiderpanel logs
#   spiderpanel uninstall
#
# FEATURES:
#   - curl | bash safe
#   - automatic sudo
#   - Python 3.12 isolated runtime
#   - destroys incompatible Python 3.14 venv automatically
#   - Arch / Omarchy / CachyOS
#   - Ubuntu / Debian
#   - Fedora / RHEL
#   - GitHub Codespaces
#   - VPS + systemd
#   - non-systemd standalone mode
#   - fixed port 8080
#   - persistent data
#   - persistent admin password
#   - Xray
#   - MTProxy
#   - global spiderpanel command
#   - update
#   - uninstall
# ============================================================

set +e

APP_NAME="SpiderPanel"

APP_DIR="${SPIDER_APP_DIR:-/opt/SpiderPanel}"
ENV_FILE="/etc/spider-panel.env"

REPO="${SPIDER_REPO:-https://github.com/amirh00sain/SpiderPanel.git}"
BRANCH="${SPIDER_BRANCH:-main}"

INSTALLER_URL="${SPIDER_INSTALLER_URL:-https://raw.githubusercontent.com/amirh00sain/SpiderPanel/main/start.sh}"

SERVICE_NAME="spider-panel"

PORT="${SPIDER_PORT:-8080}"

VENV_DIR="$APP_DIR/.venv"

PID_FILE="$APP_DIR/spiderpanel.pid"
LOG_FILE="$APP_DIR/spiderpanel.log"

XRAY_VERSION="${XRAY_VERSION:-26.3.27}"
XRAY_DIR="$APP_DIR/xray"
XRAY_BIN="$XRAY_DIR/xray"

MTPROXY_BIN="/usr/local/bin/mtproto-proxy"

CLI_PATH="/usr/local/bin/spiderpanel"

TMP_ROOT=""
TMP_INSTALLER=""

# ============================================================
# LOGGING
# ============================================================

log() {
    printf '\033[1;36m[SpiderPanel]\033[0m %s\n' "$*"
}

ok() {
    printf '\033[1;32m[OK]\033[0m %s\n' "$*"
}

warn() {
    printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2
}

error() {
    printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2
}

fail() {
    error "$*"
    exit 1
}

# ============================================================
# CLEANUP
# ============================================================

cleanup() {
    if [[ -n "${TMP_ROOT:-}" && -d "${TMP_ROOT:-}" ]]; then
        rm -rf "$TMP_ROOT" 2>/dev/null || true
    fi

    if [[ -n "${TMP_INSTALLER:-}" && -f "${TMP_INSTALLER:-}" ]]; then
        rm -f "$TMP_INSTALLER" 2>/dev/null || true
    fi
}

trap cleanup EXIT

# ============================================================
# ROOT
# ============================================================

become_root() {

    if [[ "$(id -u)" -eq 0 ]]; then
        export HOME=/root
        export XDG_CONFIG_HOME=/root/.config
        export XDG_CACHE_HOME=/root/.cache
        return 0
    fi

    command -v sudo >/dev/null 2>&1 || \
        fail "sudo is not installed."

    TMP_INSTALLER="$(mktemp /tmp/spiderpanel-start.XXXXXX.sh)" || \
        fail "Could not create temporary installer."

    log "Downloading installer for automatic sudo execution..."

    curl -fsSL \
        --retry 3 \
        --connect-timeout 10 \
        --max-time 180 \
        "$INSTALLER_URL" \
        -o "$TMP_INSTALLER" || \
        fail "Could not download installer."

    chmod 700 "$TMP_INSTALLER"

    export SPIDER_REPO="$REPO"
    export SPIDER_BRANCH="$BRANCH"
    export SPIDER_APP_DIR="$APP_DIR"
    export SPIDER_PORT="$PORT"
    export XRAY_VERSION="$XRAY_VERSION"
    export SPIDER_INSTALLER_URL="$INSTALLER_URL"

    exec sudo -E env \
        HOME=/root \
        XDG_CONFIG_HOME=/root/.config \
        XDG_CACHE_HOME=/root/.cache \
        SPIDER_REPO="$REPO" \
        SPIDER_BRANCH="$BRANCH" \
        SPIDER_APP_DIR="$APP_DIR" \
        SPIDER_PORT="$PORT" \
        XRAY_VERSION="$XRAY_VERSION" \
        SPIDER_INSTALLER_URL="$INSTALLER_URL" \
        bash "$TMP_INSTALLER" "$@"
}

# ============================================================
# OS DETECTION
# ============================================================

detect_os() {

    [[ -f /etc/os-release ]] || \
        fail "/etc/os-release not found."

    # shellcheck disable=SC1091
    source /etc/os-release

    OS_ID="${ID:-unknown}"
    PRETTY_NAME="${PRETTY_NAME:-$OS_ID}"
}

# ============================================================
# ENVIRONMENT DETECTION
# ============================================================

detect_environment() {

    IS_CODESPACE=0
    HAS_SYSTEMD=0

    if [[ "${CODESPACES:-}" == "true" || -n "${CODESPACE_NAME:-}" ]]; then
        IS_CODESPACE=1
    fi

    if command -v systemctl >/dev/null 2>&1; then

        PID1="$(ps -p 1 -o comm= 2>/dev/null | tr -d '[:space:]' || true)"

        if [[ "$PID1" == "systemd" ]]; then
            HAS_SYSTEMD=1
        fi
    fi
}

# ============================================================
# PACKAGE INSTALLERS
# ============================================================

install_apt() {

    export DEBIAN_FRONTEND=noninteractive

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
}

install_dnf() {

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
        jq
}

install_yum() {

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
}

install_pacman() {

    pacman -Sy --noconfirm --needed \
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
}

install_packages() {

    log "Installing system dependencies..."

    case "$OS_ID" in

        ubuntu|debian|linuxmint|pop|elementary)
            install_apt
            ;;

        fedora|rhel|rocky|almalinux|centos)
            if command -v dnf >/dev/null 2>&1; then
                install_dnf
            else
                install_yum
            fi
            ;;

        arch|cachyos|endeavouros|manjaro|omarchy)
            install_pacman
            ;;

        *)
            if command -v apt-get >/dev/null 2>&1; then
                install_apt
            elif command -v dnf >/dev/null 2>&1; then
                install_dnf
            elif command -v yum >/dev/null 2>&1; then
                install_yum
            elif command -v pacman >/dev/null 2>&1; then
                install_pacman
            else
                fail "Unsupported distribution: $OS_ID"
            fi
            ;;
    esac

    ok "System dependencies installed."
}

# ============================================================
# DOCKER
# ============================================================

install_docker() {

    # Codespaces already provide their own container environment.
    if [[ "$IS_CODESPACE" -eq 1 ]]; then
        log "Codespace detected: skipping Docker installation."
        return 0
    fi

    if command -v docker >/dev/null 2>&1; then
        ok "Docker already installed."
        return 0
    fi

    if [[ "$HAS_SYSTEMD" -ne 1 ]]; then
        warn "systemd unavailable; Docker setup skipped."
        return 0
    fi

    case "$OS_ID" in

        ubuntu|debian|linuxmint|pop|elementary)

            install -m 0755 -d /etc/apt/keyrings

            if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then

                if curl -fsSL \
                    "https://download.docker.com/linux/$OS_ID/gpg" |
                    gpg --dearmor \
                    -o /etc/apt/keyrings/docker.gpg; then

                    chmod a+r /etc/apt/keyrings/docker.gpg

                else

                    warn "Docker GPG key download failed."
                    return 0
                fi
            fi

            . /etc/os-release

            DOCKER_ARCH="$(dpkg --print-architecture)"
            DOCKER_CODENAME="${VERSION_CODENAME:-}"

            if [[ -z "$DOCKER_CODENAME" ]]; then
                DOCKER_CODENAME="$(lsb_release -cs 2>/dev/null || true)"
            fi

            if [[ -n "$DOCKER_CODENAME" ]]; then

                cat > /etc/apt/sources.list.d/docker.list <<EOF
deb [arch=$DOCKER_ARCH signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$OS_ID $DOCKER_CODENAME stable
EOF

                apt-get update -y

                apt-get install -y \
                    docker-ce \
                    docker-ce-cli \
                    containerd.io \
                    docker-buildx-plugin \
                    docker-compose-plugin \
                    || warn "Docker installation failed."

                systemctl enable --now docker.service 2>/dev/null || true

                command -v docker >/dev/null 2>&1 &&
                    ok "Docker installed."

            fi
            ;;

        arch|cachyos|endeavouros|manjaro|omarchy)

            pacman -S --noconfirm --needed \
                docker \
                docker-compose \
                || true

            if command -v docker >/dev/null 2>&1; then

                systemctl enable --now docker.service \
                    2>/dev/null || true

                ok "Docker installed."
            fi
            ;;

        fedora|rhel|rocky|almalinux|centos)

            if command -v dnf >/dev/null 2>&1; then
                dnf install -y docker || true
            else
                yum install -y docker || true
            fi

            if command -v docker >/dev/null 2>&1; then

                systemctl enable --now docker.service \
                    2>/dev/null || true

                ok "Docker installed."
            fi
            ;;
    esac
}

# ============================================================
# DOWNLOAD SOURCE
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
        fail "Could not clone $REPO"

    [[ -f "$TMP_ROOT/app/main.py" ]] || \
        fail "main.py not found."

    [[ -f "$TMP_ROOT/app/requirements.txt" ]] || \
        fail "requirements.txt not found."

    ok "Repository downloaded."
}

# ============================================================
# DEPLOY SOURCE
# ============================================================

deploy_application() {

    mkdir -p "$APP_DIR"

    PERSIST="$TMP_ROOT/persist"

    mkdir -p "$PERSIST"

    # Preserve database/data.
    if [[ -d "$APP_DIR/data" ]]; then
        cp -a "$APP_DIR/data" "$PERSIST/data" 2>/dev/null || true
    fi

    # Preserve local .env.
    if [[ -f "$APP_DIR/.env" ]]; then
        cp -f "$APP_DIR/.env" "$PERSIST/.env" 2>/dev/null || true
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
        "$APP_DIR/" || \
        fail "Could not copy application files."

    if [[ -d "$PERSIST/data" ]]; then

        rm -rf "$APP_DIR/data"

        cp -a \
            "$PERSIST/data" \
            "$APP_DIR/data"
    fi

    if [[ -f "$PERSIST/.env" ]]; then

        cp -f \
            "$PERSIST/.env" \
            "$APP_DIR/.env"
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
# ============================================================

install_uv() {

    UV_BIN="/usr/local/bin/uv"

    if [[ -x "$UV_BIN" ]]; then
        ok "uv already installed."
        return 0
    fi

    log "Installing uv..."

    curl -LsSf \
        https://astral.sh/uv/install.sh |
        env UV_UNMANAGED_INSTALL="/usr/local" \
        sh || \
        fail "Could not install uv."

    [[ -x "$UV_BIN" ]] || \
        fail "uv installation failed."

    ok "uv installed."
}

# ============================================================
# PYTHON 3.12
# ============================================================

setup_python() {

    log "Preparing dedicated Python 3.12 runtime..."

    install_uv

    UV_BIN="/usr/local/bin/uv"

    # --------------------------------------------------------
    # IMPORTANT:
    # Never create SpiderPanel venv using system python.
    # Omarchy / Arch may provide Python 3.14.
    # --------------------------------------------------------

    log "Installing Python 3.12 with uv..."

    "$UV_BIN" python install 3.12 || \
        fail "Python 3.12 installation failed."

    PY312="$(
        "$UV_BIN" python find 3.12 2>/dev/null |
        head -n1 || true
    )"

    [[ -n "$PY312" && -x "$PY312" ]] || \
        fail "Python 3.12 runtime was not found."

    PY312_VERSION="$(
        "$PY312" \
        -c \
        'import sys; print(".".join(map(str,sys.version_info[:3])))'
    )"

    log "Selected Python: $PY312_VERSION"

    [[ "$PY312_VERSION" == 3.12.* ]] || \
        fail "Wrong Python runtime: $PY312_VERSION"

    # --------------------------------------------------------
    # Detect and destroy incompatible venv.
    # --------------------------------------------------------

    RECREATE_VENV=0

    if [[ ! -x "$VENV_DIR/bin/python" ]]; then

        RECREATE_VENV=1

    else

        EXISTING_VERSION="$(
            "$VENV_DIR/bin/python" \
            -c \
            'import sys; print(".".join(map(str,sys.version_info[:2])))' \
            2>/dev/null || true
        )"

        if [[ "$EXISTING_VERSION" != "3.12" ]]; then

            warn "Existing venv uses Python $EXISTING_VERSION."

            log "Removing incompatible venv..."

            RECREATE_VENV=1
        fi
    fi

    if [[ "$RECREATE_VENV" -eq 1 ]]; then

        rm -rf "$VENV_DIR"

        log "Creating Python 3.12 virtual environment..."

        "$UV_BIN" venv \
            --python "$PY312" \
            "$VENV_DIR" || \
            fail "Could not create Python 3.12 venv."

    fi

    [[ -x "$VENV_DIR/bin/python" ]] || \
        fail "Venv Python missing."

    # --------------------------------------------------------
    # Make absolutely sure it is 3.12.
    # --------------------------------------------------------

    FINAL_VERSION="$(
        "$VENV_DIR/bin/python" \
        -c \
        'import sys; print(".".join(map(str,sys.version_info[:2])))'
    )"

    [[ "$FINAL_VERSION" == "3.12" ]] || \
        fail "Venv was created with Python $FINAL_VERSION instead of 3.12."

    # --------------------------------------------------------
    # Python package installation
    # --------------------------------------------------------

    mkdir -p /root/.cache/pip

    export PIP_DISABLE_PIP_VERSION_CHECK=1
    export PIP_NO_CACHE_DIR=1

    log "Upgrading pip..."

    "$VENV_DIR/bin/python" \
        -m pip install \
        --upgrade \
        pip \
        setuptools \
        wheel || \
        fail "Could not upgrade pip."

    log "Installing SpiderPanel Python dependencies..."

    "$VENV_DIR/bin/python" \
        -m pip install \
        --no-cache-dir \
        -r "$APP_DIR/requirements.txt" || \
        fail "Python dependency installation failed."

    # --------------------------------------------------------
    # Dependency verification
    # --------------------------------------------------------

    "$VENV_DIR/bin/python" \
        -c 'import fastapi' || \
        fail "FastAPI verification failed."

    "$VENV_DIR/bin/python" \
        -c 'import uvicorn' || \
        fail "Uvicorn verification failed."

    "$VENV_DIR/bin/python" \
        -c 'import PIL' || \
        fail "Pillow verification failed."

    "$VENV_DIR/bin/python" \
        -c 'import httpx' || \
        fail "HTTPX verification failed."

    echo
    ok "Python runtime:"
    "$VENV_DIR/bin/python" --version

    echo
    ok "Installed Pillow:"
    "$VENV_DIR/bin/python" \
        -c 'import PIL; print(PIL.__version__)'

    echo
    ok "Python environment ready."
}

# ============================================================
# XRAY
# ============================================================

install_xray() {

    mkdir -p "$XRAY_DIR"

    if [[ -x "$XRAY_BIN" ]]; then
        ok "Existing Xray preserved."
        return 0
    fi

    log "Installing Xray-core v$XRAY_VERSION..."

    XRAY_ZIP="$TMP_ROOT/xray.zip"

    XRAY_URL="https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-linux-64.zip"

    if ! curl -fL \
        --retry 3 \
        --connect-timeout 10 \
        --max-time 180 \
        "$XRAY_URL" \
        -o "$XRAY_ZIP"; then

        warn "Xray download failed. SpiderPanel can download it later."
        return 0
    fi

    unzip -oq \
        "$XRAY_ZIP" \
        -d "$XRAY_DIR" || {

        warn "Xray extraction failed."
        return 0
    }

    if [[ ! -f "$XRAY_BIN" ]]; then

        FOUND_XRAY="$(
            find "$XRAY_DIR" \
                -maxdepth 1 \
                -type f \
                -name xray \
                -print \
                -quit \
                2>/dev/null || true
        )"

        if [[ -n "$FOUND_XRAY" ]]; then
            mv -f "$FOUND_XRAY" "$XRAY_BIN"
        fi
    fi

    if [[ -f "$XRAY_BIN" ]]; then

        chmod 755 "$XRAY_BIN"

        rm -f \
            "$XRAY_DIR"/*.dat \
            "$XRAY_DIR"/*.zip \
            2>/dev/null || true

        ok "Xray installed at $XRAY_BIN"

    else

        warn "Xray binary was not found."

    fi
}

# ============================================================
# MTPROXY
# ============================================================

install_mtproxy() {

    if [[ -x "$MTPROXY_BIN" ]]; then
        ok "Existing MTProxy preserved."
        return 0
    fi

    log "Building Telegram MTProxy..."

    MTPROXY_SRC="$TMP_ROOT/MTProxy"

    if ! git clone \
        --depth 1 \
        https://github.com/TelegramMessenger/MTProxy.git \
        "$MTPROXY_SRC"; then

        warn "Could not download MTProxy."
        return 0
    fi

    if ! make \
        -C "$MTPROXY_SRC" \
        -j"$(nproc 2>/dev/null || echo 2)"; then

        warn "MTProxy compilation failed."
        return 0
    fi

    if [[ -x "$MTPROXY_SRC/objs/bin/mtproto-proxy" ]]; then

        install \
            -m 0755 \
            "$MTPROXY_SRC/objs/bin/mtproto-proxy" \
            "$MTPROXY_BIN"

        ok "MTProxy installed at $MTPROXY_BIN"

    else

        warn "MTProxy binary not found after build."

    fi
}

# ============================================================
# ENVIRONMENT
# ============================================================

generate_secret() {

    "$VENV_DIR/bin/python" - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
}

generate_password() {

    "$VENV_DIR/bin/python" - <<'PY'
import secrets
print(secrets.token_urlsafe(18))
PY
}

get_env_value() {

    local KEY="$1"

    if [[ -f "$ENV_FILE" ]]; then

        grep "^${KEY}=" "$ENV_FILE" 2>/dev/null |
            head -n1 |
            cut -d= -f2- || true
    fi
}

setup_environment() {

    if [[ ! -f "$ENV_FILE" ]]; then

        SECRET_KEY="$(generate_secret)"
        ADMIN_PASSWORD="$(generate_password)"

        cat > "$ENV_FILE" <<EOF
SECRET_KEY=$SECRET_KEY
ADMIN_PASSWORD=$ADMIN_PASSWORD

DATA_DIR=$APP_DIR/data
SPIDER_DATA_DIR=$APP_DIR/data

PORT=$PORT

MTPROTO_PROXY_BIN=$MTPROXY_BIN

WORKER_SYNC_INTERVAL=3600

RAILWAY_PUBLIC_DOMAIN=
EOF

        chmod 600 "$ENV_FILE"

        ok "Environment created."

    else

        chmod 600 "$ENV_FILE"

        if grep -q '^PORT=' "$ENV_FILE"; then
            sed -i "s/^PORT=.*/PORT=$PORT/" "$ENV_FILE"
        else
            printf 'PORT=%s\n' "$PORT" >> "$ENV_FILE"
        fi

        if ! grep -q '^DATA_DIR=' "$ENV_FILE"; then
            printf 'DATA_DIR=%s\n' "$APP_DIR/data" >> "$ENV_FILE"
        fi

        if ! grep -q '^SPIDER_DATA_DIR=' "$ENV_FILE"; then
            printf 'SPIDER_DATA_DIR=%s\n' "$APP_DIR/data" >> "$ENV_FILE"
        fi

        if ! grep -q '^MTPROTO_PROXY_BIN=' "$ENV_FILE"; then
            printf 'MTPROTO_PROXY_BIN=%s\n' "$MTPROXY_BIN" >> "$ENV_FILE"
        fi

        ok "Existing environment preserved."
    fi

    ADMIN_PASSWORD="$(get_env_value ADMIN_PASSWORD)"

    if [[ -z "$ADMIN_PASSWORD" ]]; then

        ADMIN_PASSWORD="$(generate_password)"

        printf 'ADMIN_PASSWORD=%s\n' "$ADMIN_PASSWORD" >> "$ENV_FILE"

        chmod 600 "$ENV_FILE"
    fi

    cat > "$APP_DIR/INSTALL-CREDENTIALS.txt" <<EOF
SpiderPanel initial admin password:
$ADMIN_PASSWORD

Environment file:
$ENV_FILE

Application directory:
$APP_DIR

Application port:
$PORT
EOF

    chmod 600 "$APP_DIR/INSTALL-CREDENTIALS.txt"

    ok "Credentials saved."
}

# ============================================================
# VALIDATION
# ============================================================

validate_application() {

    log "Validating SpiderPanel Python source..."

    cd "$APP_DIR"

    "$VENV_DIR/bin/python" \
        -m compileall \
        -q \
        "$APP_DIR" || \
        fail "Python syntax validation failed."

    ok "Python syntax validation passed."
}

# ============================================================
# PORT
# ============================================================

port_in_use() {

    if command -v ss >/dev/null 2>&1; then

        ss -lntp 2>/dev/null |
            grep -Eq ":${PORT}[[:space:]]"

    elif command -v lsof >/dev/null 2>&1; then

        lsof \
            -iTCP:"$PORT" \
            -sTCP:LISTEN \
            >/dev/null 2>&1

    else

        return 1
    fi
}

port_owner() {

    if command -v lsof >/dev/null 2>&1; then

        lsof \
            -nP \
            -iTCP:"$PORT" \
            -sTCP:LISTEN \
            2>/dev/null || true

    elif command -v ss >/dev/null 2>&1; then

        ss -lntp 2>/dev/null |
            grep -E ":${PORT}[[:space:]]" || true
    fi
}

# ============================================================
# SYSTEMD
# ============================================================

create_systemd_service() {

    command -v systemctl >/dev/null 2>&1 || \
        fail "systemctl not available."

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

    systemctl enable \
        "$SERVICE_NAME.service" \
        >/dev/null

    ok "systemd service created."
}

# ============================================================
# STANDALONE
# ============================================================

stop_standalone() {

    if [[ -f "$PID_FILE" ]]; then

        PID="$(cat "$PID_FILE" 2>/dev/null || true)"

        if [[ "$PID" =~ ^[0-9]+$ ]]; then

            if kill -0 "$PID" 2>/dev/null; then

                kill "$PID" 2>/dev/null || true

                for _ in {1..20}; do

                    kill -0 "$PID" 2>/dev/null || break

                    sleep 0.25
                done

                kill -9 "$PID" 2>/dev/null || true
            fi
        fi

        rm -f "$PID_FILE"
    fi
}

start_standalone() {

    stop_standalone

    if port_in_use; then

        warn "Port $PORT is already occupied."

        port_owner

        return 1
    fi

    touch "$LOG_FILE"

    chmod 640 "$LOG_FILE"

    cd "$APP_DIR"

    nohup \
        "$VENV_DIR/bin/uvicorn" \
        main:app \
        --host 0.0.0.0 \
        --port "$PORT" \
        >> "$LOG_FILE" \
        2>&1 &

    APP_PID="$!"

    echo "$APP_PID" > "$PID_FILE"

    chmod 640 "$PID_FILE"

    sleep 4

    if kill -0 "$APP_PID" 2>/dev/null; then

        ok "SpiderPanel started. PID=$APP_PID"

        return 0
    fi

    error "SpiderPanel failed to start."

    tail -n 120 "$LOG_FILE" 2>/dev/null || true

    rm -f "$PID_FILE"

    return 1
}

# ============================================================
# START
# ============================================================

start_panel() {

    if [[ "$HAS_SYSTEMD" -eq 1 ]]; then

        if systemctl is-active \
            --quiet \
            "$SERVICE_NAME.service"; then

            ok "SpiderPanel is already running."

            return 0
        fi

        if port_in_use; then

            warn "Port $PORT is already occupied."

            port_owner

            return 1
        fi

        systemctl restart \
            "$SERVICE_NAME.service"

        sleep 4

        if systemctl is-active \
            --quiet \
            "$SERVICE_NAME.service"; then

            ok "SpiderPanel is running via systemd."

            return 0
        fi

        error "SpiderPanel failed to start."

        journalctl \
            -u "$SERVICE_NAME.service" \
            -n 120 \
            --no-pager || true

        return 1

    else

        start_standalone
    fi
}

# ============================================================
# STOP
# ============================================================

stop_panel() {

    if [[ "$HAS_SYSTEMD" -eq 1 ]]; then

        systemctl stop \
            "$SERVICE_NAME.service" \
            >/dev/null 2>&1 || true

    else

        stop_standalone
    fi

    ok "SpiderPanel stopped."
}

# ============================================================
# INFO
# ============================================================

get_public_ip() {

    curl \
        -4 \
        -fsS \
        --max-time 5 \
        https://api.ipify.org \
        2>/dev/null || true
}

get_local_ip() {

    ip -4 route get 1.1.1.1 2>/dev/null |
        awk '
        {
            for(i=1;i<=NF;i++) {
                if($i=="src") {
                    print $(i+1)
                    exit
                }
            }
        }' || true
}

get_codespace_url() {

    if [[ "$IS_CODESPACE" -eq 1 && -n "${CODESPACE_NAME:-}" ]]; then

        DOMAIN="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"

        printf \
            'https://%s-%s.%s/spider\n' \
            "$CODESPACE_NAME" \
            "$PORT" \
            "$DOMAIN"
    fi
}

info_panel() {

    ADMIN_PASSWORD="$(get_env_value ADMIN_PASSWORD)"

    PUBLIC_IP="$(get_public_ip)"
    LOCAL_IP="$(get_local_ip)"
    CODESPACE_URL="$(get_codespace_url)"

    echo
    echo "============================================================"
    echo "                    SPIDERPANEL"
    echo "============================================================"
    echo

    echo "Application : $APP_DIR"
    echo "Environment : $ENV_FILE"
    echo "Port        : $PORT"

    if [[ "$HAS_SYSTEMD" -eq 1 ]]; then

        echo "Mode        : systemd"
        echo "Service     : $SERVICE_NAME.service"

    else

        echo "Mode        : standalone"
        echo "PID file    : $PID_FILE"
        echo "Log file    : $LOG_FILE"

    fi

    echo

    if [[ -n "$PUBLIC_IP" ]]; then
        echo "Public URL  : http://${PUBLIC_IP}:${PORT}/spider"
    fi

    if [[ -n "$LOCAL_IP" ]]; then
        echo "Local URL   : http://${LOCAL_IP}:${PORT}/spider"
    fi

    if [[ -n "$CODESPACE_URL" ]]; then
        echo "Codespace   : $CODESPACE_URL"
    fi

    echo

    echo "Admin password:"
    echo "${ADMIN_PASSWORD:-NOT FOUND}"

    echo

    echo "Credentials : $APP_DIR/INSTALL-CREDENTIALS.txt"

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

    echo "============================================================"
    echo
}

# ============================================================
# STATUS
# ============================================================

status_panel() {

    echo
    echo "============================================================"
    echo "                    SpiderPanel Status"
    echo "============================================================"
    echo

    echo "Application : $APP_DIR"
    echo "Port        : $PORT"

    if [[ "$HAS_SYSTEMD" -eq 1 ]]; then

        echo "Mode        : systemd"

        if systemctl is-active \
            --quiet \
            "$SERVICE_NAME.service"; then

            echo "Status      : RUNNING"

        else

            echo "Status      : STOPPED"

        fi

        echo

        systemctl status \
            "$SERVICE_NAME.service" \
            --no-pager \
            || true

    else

        echo "Mode        : standalone"

        if [[ -f "$PID_FILE" ]]; then

            PID="$(cat "$PID_FILE" 2>/dev/null || true)"

            if [[ "$PID" =~ ^[0-9]+$ ]] &&
               kill -0 "$PID" 2>/dev/null; then

                echo "Status      : RUNNING"
                echo "PID         : $PID"

            else

                echo "Status      : STOPPED"

            fi

        else

            echo "Status      : STOPPED"

        fi

        echo "Log         : $LOG_FILE"

    fi

    echo
}

# ============================================================
# LOGS
# ============================================================

logs_panel() {

    if [[ "$HAS_SYSTEMD" -eq 1 ]]; then

        journalctl \
            -u "$SERVICE_NAME.service" \
            -f

    else

        touch "$LOG_FILE"

        tail -f "$LOG_FILE"

    fi
}

# ============================================================
# UPDATE
# ============================================================

update_panel() {

    log "Downloading newest SpiderPanel installer..."

    exec bash <(
        curl -fsSL \
            --retry 3 \
            --connect-timeout 10 \
            --max-time 180 \
            "$INSTALLER_URL"
    ) install
}

# ============================================================
# UNINSTALL
# ============================================================

uninstall_panel() {

    echo
    echo "============================================================"
    echo "WARNING"
    echo "============================================================"
    echo
    echo "This will remove:"
    echo
    echo "  $APP_DIR"
    echo "  $ENV_FILE"
    echo "  $SERVICE_NAME.service"
    echo "  $CLI_PATH"
    echo
    echo "SpiderPanel data will also be removed from $APP_DIR."
    echo
    read -r -p "Type REMOVE to continue: " CONFIRM

    if [[ "$CONFIRM" != "REMOVE" ]]; then
        echo "Cancelled."
        return 0
    fi

    stop_panel || true

    if [[ "$HAS_SYSTEMD" -eq 1 ]]; then

        systemctl disable \
            "$SERVICE_NAME.service" \
            >/dev/null 2>&1 || true

        rm -f \
            "/etc/systemd/system/${SERVICE_NAME}.service"

        systemctl daemon-reload || true
    fi

    rm -f "$PID_FILE" 2>/dev/null || true
    rm -f "$ENV_FILE" 2>/dev/null || true
    rm -f "$CLI_PATH" 2>/dev/null || true

    rm -rf "$APP_DIR"

    ok "SpiderPanel completely removed."
}

# ============================================================
# CREATE GLOBAL COMMAND
# ============================================================

create_cli() {

    cat > "$CLI_PATH" <<'EOF'
#!/usr/bin/env bash

set -Eeuo pipefail

APP_DIR="/opt/SpiderPanel"
ENV_FILE="/etc/spider-panel.env"

SERVICE_NAME="spider-panel"

PORT="8080"

PID_FILE="$APP_DIR/spiderpanel.pid"
LOG_FILE="$APP_DIR/spiderpanel.log"

SYSTEMD=0
CODESPACE=0

if [[ "${CODESPACES:-}" == "true" || -n "${CODESPACE_NAME:-}" ]]; then
    CODESPACE=1
fi

if command -v systemctl >/dev/null 2>&1; then

    PID1="$(ps -p 1 -o comm= 2>/dev/null | tr -d '[:space:]' || true)"

    if [[ "$PID1" == "systemd" ]]; then
        SYSTEMD=1
    fi
fi

get_env() {

    local KEY="$1"

    if [[ -f "$ENV_FILE" ]]; then

        grep "^${KEY}=" \
            "$ENV_FILE" \
            2>/dev/null |
            head -n1 |
            cut -d= -f2- || true
    fi
}

get_public_ip() {

    curl \
        -4 \
        -fsS \
        --max-time 5 \
        https://api.ipify.org \
        2>/dev/null || true
}

get_local_ip() {

    ip -4 route get 1.1.1.1 2>/dev/null |
        awk '
        {
            for(i=1;i<=NF;i++) {
                if($i=="src") {
                    print $(i+1)
                    exit
                }
            }
        }' || true
}

get_codespace_url() {

    if [[ "$CODESPACE" -eq 1 && -n "${CODESPACE_NAME:-}" ]]; then

        DOMAIN="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"

        printf \
            'https://%s-%s.%s/spider\n' \
            "$CODESPACE_NAME" \
            "$PORT" \
            "$DOMAIN"
    fi
}

running() {

    if [[ "$SYSTEMD" -eq 1 ]]; then

        systemctl is-active \
            --quiet \
            "$SERVICE_NAME.service"

    else

        [[ -f "$PID_FILE" ]] || return 1

        PID="$(cat "$PID_FILE" 2>/dev/null || true)"

        [[ "$PID" =~ ^[0-9]+$ ]] || return 1

        kill -0 "$PID" 2>/dev/null
    fi
}

start_panel() {

    if [[ "$SYSTEMD" -eq 1 ]]; then

        systemctl restart \
            "$SERVICE_NAME.service"

    else

        if running; then

            echo "SpiderPanel is already running."

            return 0
        fi

        mkdir -p "$APP_DIR"

        nohup \
            "$APP_DIR/.venv/bin/uvicorn" \
            main:app \
            --host 0.0.0.0 \
            --port "$PORT" \
            >> "$LOG_FILE" \
            2>&1 &

        echo "$!" > "$PID_FILE"

    fi

    sleep 3

    if running; then

        echo "SpiderPanel started."

    else

        echo "SpiderPanel failed to start."

        if [[ "$SYSTEMD" -eq 1 ]]; then

            journalctl \
                -u "$SERVICE_NAME.service" \
                -n 100 \
                --no-pager || true

        else

            tail -n 100 "$LOG_FILE" \
                2>/dev/null || true
        fi

        exit 1
    fi
}

stop_panel() {

    if [[ "$SYSTEMD" -eq 1 ]]; then

        systemctl stop \
            "$SERVICE_NAME.service" \
            || true

    else

        if [[ -f "$PID_FILE" ]]; then

            PID="$(cat "$PID_FILE" 2>/dev/null || true)"

            if [[ "$PID" =~ ^[0-9]+$ ]]; then
                kill "$PID" 2>/dev/null || true
            fi

            rm -f "$PID_FILE"
        fi

    fi

    echo "SpiderPanel stopped."
}

restart_panel() {

    stop_panel

    sleep 1

    start_panel
}

status_panel() {

    echo
    echo "SpiderPanel"
    echo "-----------"

    echo "Port: $PORT"

    if [[ "$SYSTEMD" -eq 1 ]]; then
        echo "Mode: systemd"
    else
        echo "Mode: standalone"
    fi

    if running; then
        echo "Status: RUNNING"
    else
        echo "Status: STOPPED"
    fi

    echo
}

info_panel() {

    PASSWORD="$(get_env ADMIN_PASSWORD)"

    PUBLIC_IP="$(get_public_ip)"
    LOCAL_IP="$(get_local_ip)"
    CODE_URL="$(get_codespace_url)"

    echo
    echo "============================================================"
    echo "                    SPIDERPANEL"
    echo "============================================================"
    echo

    echo "Application : $APP_DIR"
    echo "Environment : $ENV_FILE"
    echo "Port        : $PORT"

    if [[ "$SYSTEMD" -eq 1 ]]; then
        echo "Mode        : systemd"
        echo "Service     : $SERVICE_NAME.service"
    else
        echo "Mode        : standalone"
        echo "PID file    : $PID_FILE"
        echo "Log file    : $LOG_FILE"
    fi

    echo

    if [[ -n "$PUBLIC_IP" ]]; then
        echo "Public URL  : http://${PUBLIC_IP}:${PORT}/spider"
    fi

    if [[ -n "$LOCAL_IP" ]]; then
        echo "Local URL   : http://${LOCAL_IP}:${PORT}/spider"
    fi

    if [[ -n "$CODE_URL" ]]; then
        echo "Codespace   : $CODE_URL"
    fi

    echo

    echo "Admin password:"
    echo "${PASSWORD:-NOT FOUND}"

    echo

    echo "Credentials : $APP_DIR/INSTALL-CREDENTIALS.txt"

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
    echo "============================================================"
    echo
}

logs_panel() {

    if [[ "$SYSTEMD" -eq 1 ]]; then

        journalctl \
            -u "$SERVICE_NAME.service" \
            -f

    else

        touch "$LOG_FILE"

        tail -f "$LOG_FILE"

    fi
}

update_panel() {

    curl \
        -fsSL \
        --retry 3 \
        --connect-timeout 10 \
        --max-time 180 \
        "https://raw.githubusercontent.com/amirh00sain/SpiderPanel/main/start.sh" |
        bash
}

uninstall_panel() {

    echo
    echo "WARNING: SpiderPanel will be deleted."
    echo

    read -r \
        -p "Type REMOVE to continue: " \
        CONFIRM

    if [[ "$CONFIRM" != "REMOVE" ]]; then
        echo "Cancelled."
        exit 0
    fi

    stop_panel || true

    if [[ "$SYSTEMD" -eq 1 ]]; then

        systemctl disable \
            "$SERVICE_NAME.service" \
            >/dev/null 2>&1 || true

        rm -f \
            "/etc/systemd/system/${SERVICE_NAME}.service"

        systemctl daemon-reload || true
    fi

    rm -f "$PID_FILE" 2>/dev/null || true
    rm -f "$ENV_FILE" 2>/dev/null || true
    rm -rf "$APP_DIR"
    rm -f "/usr/local/bin/spiderpanel"

    echo
    echo "SpiderPanel removed."
    echo
}

menu() {

    while true; do

        clear 2>/dev/null || true

        echo
        echo "============================================================"
        echo "                    SPIDERPANEL"
        echo "============================================================"
        echo
        echo "  1) Info"
        echo "  2) Status"
        echo "  3) Start"
        echo "  4) Stop"
        echo "  5) Restart"
        echo "  6) Update"
        echo "  7) Logs"
        echo "  8) Uninstall"
        echo "  0) Exit"
        echo

        printf "Select: "

        read -r CHOICE

        case "$CHOICE" in

            1)
                info_panel
                read -r -p "Press Enter..."
                ;;

            2)
                status_panel
                read -r -p "Press Enter..."
                ;;

            3)
                start_panel
                read -r -p "Press Enter..."
                ;;

            4)
                stop_panel
                read -r -p "Press Enter..."
                ;;

            5)
                restart_panel
                read -r -p "Press Enter..."
                ;;

            6)
                update_panel
                read -r -p "Press Enter..."
                ;;

            7)
                logs_panel
                ;;

            8)
                uninstall_panel
                exit 0
                ;;

            0)
                exit 0
                ;;

            *)
                echo "Invalid option."
                sleep 1
                ;;
        esac

    done
}

COMMAND="${1:-menu}"

case "$COMMAND" in

    menu)
        menu
        ;;

    info)
        info_panel
        ;;

    status)
        status_panel
        ;;

    start)
        start_panel
        ;;

    stop)
        stop_panel
        ;;

    restart)
        restart_panel
        ;;

    update)
        update_panel
        ;;

    logs)
        logs_panel
        ;;

    uninstall|remove|delete)
        uninstall_panel
        ;;

    *)
        echo
        echo "SpiderPanel"
        echo
        echo "Usage:"
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
        ;;
esac
EOF

    chmod 755 "$CLI_PATH"

    ok "Global command installed: spiderpanel"
}

# ============================================================
# INSTALL
# ============================================================

install_panel() {

    become_root "$@"

    detect_os
    detect_environment

    echo
    log "Installing $APP_NAME..."
    echo

    log "OS: $PRETTY_NAME"

    if [[ "$IS_CODESPACE" -eq 1 ]]; then

        log "Environment: GitHub Codespaces"

    elif [[ "$HAS_SYSTEMD" -eq 1 ]]; then

        log "Environment: Linux + systemd"

    else

        log "Environment: Linux without systemd"

    fi

    echo

    install_packages

    install_docker

    download_source

    deploy_application

    setup_python

    install_xray

    install_mtproxy

    setup_environment

    validate_application

    if [[ "$HAS_SYSTEMD" -eq 1 ]]; then

        create_systemd_service

    fi

    start_panel || \
        fail "SpiderPanel could not be started."

    create_cli

    hash -r 2>/dev/null || true

    echo
    ok "SpiderPanel installation completed."
    echo

    info_panel
}

# ============================================================
# MAIN
# ============================================================

COMMAND="${1:-install}"

case "$COMMAND" in

    install)

        install_panel "$@"

        ;;

    *)

        become_root "$@"

        detect_os
        detect_environment

        if [[ -x "$CLI_PATH" ]]; then

            exec "$CLI_PATH" "$@"

        else

            install_panel install

        fi

        ;;

esac
