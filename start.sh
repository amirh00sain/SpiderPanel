#!/usr/bin/env bash
set -Eeuo pipefail

# SpiderPanel universal installer/manager
# Generated as a single self-contained script.

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
TMP_ROOT=""

log(){ printf '[SpiderPanel] %s\n' "$*"; }
ok(){ printf '[OK] %s\n' "$*"; }
warn(){ printf '[WARN] %s\n' "$*" >&2; }
fail(){ printf '[ERROR] %s\n' "$*" >&2; exit 1; }
cleanup(){ [[ -z "${TMP_ROOT:-}" ]] || rm -rf "$TMP_ROOT" 2>/dev/null || true; }
trap cleanup EXIT

root(){
  if (( EUID == 0 )); then return; fi
  command -v sudo >/dev/null 2>&1 || fail 'sudo is required';
  local f; f=$(mktemp /tmp/spiderpanel-root.XXXXXX);
  curl -fsSL --retry 5 "$INSTALLER_URL" -o "$f" || fail 'cannot download installer';
  chmod 700 "$f";
  exec sudo -E bash "$f" "$@";
}

detect(){
  [[ -r /etc/os-release ]] || fail 'cannot detect operating system';
  . /etc/os-release; OS_ID="${ID:-unknown}"; OS_NAME="${PRETTY_NAME:-$ID}";
  PID1=$(ps -p 1 -o comm= 2>/dev/null | tr -d '[:space:]' || true);
  if [[ "$PID1" == systemd ]]; then HAS_SYSTEMD=1; else HAS_SYSTEMD=0; fi
  if [[ "${CODESPACES:-}" == true || -n "${CODESPACE_NAME:-}" ]]; then IS_CODESPACE=1; else IS_CODESPACE=0; fi
}

packages(){
  log 'Installing system dependencies...';
  case "$OS_ID" in
    arch|omarchy|cachyos|endeavouros|manjaro)
      pacman -Sy --noconfirm --needed ca-certificates curl git unzip xz tar gzip rsync base-devel openssl zlib procps-ng iproute2 iputils net-tools lsof jq
      ;;
    ubuntu|debian|linuxmint|pop)
      export DEBIAN_FRONTEND=noninteractive; apt-get update -y; apt-get install -y ca-certificates curl git unzip xz-utils tar gzip rsync build-essential openssl libssl-dev zlib1g-dev procps iproute2 iputils-ping net-tools lsof jq python3 python3-pip python3-venv
      ;;
    fedora|rhel|rocky|almalinux|centos)
      dnf install -y ca-certificates curl git unzip xz tar gzip rsync gcc gcc-c++ make openssl openssl-devel zlib-devel procps iproute iputils net-tools lsof jq python3 python3-pip 2>/dev/null || yum install -y ca-certificates curl git unzip xz tar gzip rsync gcc gcc-c++ make openssl openssl-devel zlib-devel procps iproute iputils net-tools lsof jq python3 python3-pip
      ;;
    *)
      if command -v pacman >/dev/null 2>&1; then pacman -Sy --noconfirm --needed ca-certificates curl git unzip xz tar gzip rsync base-devel openssl zlib procps-ng iproute2 iputils net-tools lsof jq; elif command -v apt-get >/dev/null 2>&1; then export DEBIAN_FRONTEND=noninteractive; apt-get update -y; apt-get install -y ca-certificates curl git unzip xz-utils tar gzip rsync build-essential openssl libssl-dev zlib1g-dev procps iproute2 iputils-ping net-tools lsof jq python3 python3-pip python3-venv; else fail "unsupported OS: $OS_ID"; fi
      ;;
  esac
  ok 'System dependencies installed.'
}

download_repo(){
  TMP_ROOT=$(mktemp -d /tmp/spiderpanel.XXXXXX);
  log 'Downloading SpiderPanel...';
  git clone --depth 1 --branch "$BRANCH" --single-branch "$REPO" "$TMP_ROOT/app" || fail 'git clone failed';
  [[ -f "$TMP_ROOT/app/main.py" ]] || fail 'main.py not found';
  [[ -f "$TMP_ROOT/app/requirements.txt" ]] || fail 'requirements.txt not found';
  ok 'Repository downloaded.'
}

deploy(){
  mkdir -p "$APP_DIR";
  local keep="$TMP_ROOT/keep"; mkdir -p "$keep";
  [[ ! -d "$APP_DIR/data" ]] || cp -a "$APP_DIR/data" "$keep/data";
  [[ ! -f "$APP_DIR/.env" ]] || cp -f "$APP_DIR/.env" "$keep/.env";
  log 'Installing application files...';
  rsync -a --delete --exclude '.git/' --exclude '.venv/' --exclude 'data/' --exclude '*.pid' --exclude '*.log' "$TMP_ROOT/app/" "$APP_DIR/" || fail 'application deployment failed';
  [[ ! -d "$keep/data" ]] || { rm -rf "$APP_DIR/data"; cp -a "$keep/data" "$APP_DIR/data"; };
  [[ ! -f "$keep/.env" ]] || cp -f "$keep/.env" "$APP_DIR/.env";
  mkdir -p "$APP_DIR/data" "$APP_DIR/xray"; chmod 755 "$APP_DIR";
  ok 'Application installed.'
}

install_uv(){
  if [[ -x "$UV" ]] && "$UV" --version >/dev/null 2>&1; then ok "uv ready: $($UV --version)"; return; fi
  if [[ -x /usr/local/uv/uv ]]; then ln -sf /usr/local/uv/uv "$UV"; elif [[ -x /root/.local/bin/uv ]]; then ln -sf /root/.local/bin/uv "$UV"; else
    log 'Installing uv...'; local f; f=$(mktemp /tmp/uv-install.XXXXXX); curl -LsSf https://astral.sh/uv/install.sh -o "$f" || fail 'uv download failed'; UV_UNMANAGED_INSTALL=/usr/local bash "$f" || true; rm -f "$f";
    if [[ ! -x "$UV" && -x /usr/local/uv/uv ]]; then ln -sf /usr/local/uv/uv "$UV"; fi
    if [[ ! -x "$UV" && -x /root/.local/bin/uv ]]; then ln -sf /root/.local/bin/uv "$UV"; fi
  fi
  [[ -x "$UV" ]] || fail 'uv was not found after installation';
  "$UV" --version >/dev/null 2>&1 || fail 'uv cannot execute';
  ok "uv ready: $($UV --version)"
}

setup_python(){
  install_uv; log 'Installing Python 3.12...'; "$UV" python install 3.12 || fail 'Python 3.12 install failed';
  local py; py=$("$UV" python find 3.12 | head -n1); [[ -x "$py" ]] || fail 'Python 3.12 binary not found';
  [[ "$($py -c 'import sys; print(sys.version_info[:2])')" == '(3, 12)' ]] || fail 'wrong Python selected';
  rm -rf "$VENV"; log 'Creating Python 3.12 virtual environment...'; "$UV" venv --python "$py" "$VENV" || fail 'venv creation failed';
  local p="$VENV/bin/python"; [[ -x "$p" ]] || fail 'venv Python missing';
  [[ "$($p -c 'import sys; print(sys.version_info[:2])')" == '(3, 12)' ]] || fail 'venv is not Python 3.12';
  "$p" -m pip install --upgrade --no-cache-dir pip setuptools wheel || fail 'packaging tools failed';
  log 'Installing Python dependencies...';
  "$p" -m pip install --no-cache-dir --only-binary=:all: -r "$APP_DIR/requirements.txt" || "$p" -m pip install --no-cache-dir -r "$APP_DIR/requirements.txt" || fail 'dependency installation failed';
  "$p" -c 'import fastapi,uvicorn,httpx,websockets,aiofiles,qrcode,PIL,psutil,cryptography,socks; import sys; assert sys.version_info[:2]==(3,12)' || fail 'dependency test failed';
  ok "Python runtime ready: $($p --version)"
}

generate_secret(){ openssl rand -hex 32; }
generate_password(){ openssl rand -hex 10; }

config(){
  mkdir -p "$APP_DIR/data"; local secret='' password='';
  if [[ -f "$ENV_FILE" ]]; then secret=$(grep '^SECRET_KEY=' "$ENV_FILE" | head -n1 | cut -d= -f2- || true); password=$(grep '^ADMIN_PASSWORD=' "$ENV_FILE" | head -n1 | cut -d= -f2- || true); fi
  [[ -n "$secret" ]] || secret=$(generate_secret); [[ -n "$password" ]] || password=$(generate_password);
  cat > "$ENV_FILE" <<EOF
SECRET_KEY=$secret
ADMIN_PASSWORD=$password
PORT=8080
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
  chmod 600 "$ENV_FILE";
  cat > "$APP_DIR/INSTALL-CREDENTIALS.txt" <<EOF
SpiderPanel
===========
URL: http://127.0.0.1:8080/spider
Port: 8080
Admin Password: $password
Application: $APP_DIR
Environment: $ENV_FILE
Python: $VENV/bin/python
EOF
  chmod 600 "$APP_DIR/INSTALL-CREDENTIALS.txt"; ok 'Configuration completed.'
}

install_xray(){
  local a x url zip; mkdir -p "$APP_DIR/xray"; [[ -x "$XRAY" ]] && "$XRAY" version >/dev/null 2>&1 && { ok 'Xray already installed.'; return; };
  a=$(uname -m); case "$a" in x86_64) x=64;; aarch64|arm64) x=arm64-v8a;; armv7l) x=arm32-v7a;; *) warn 'Unsupported Xray architecture'; return;; esac;
  url="https://github.com/XTLS/Xray-core/releases/download/v26.3.27/Xray-linux-$x.zip"; zip="$APP_DIR/xray.zip"; log 'Installing Xray...';
  if curl -fL --retry 5 "$url" -o "$zip"; then rm -rf "$APP_DIR/xray"; mkdir -p "$APP_DIR/xray"; unzip -o "$zip" -d "$APP_DIR/xray" >/dev/null || { rm -f "$zip"; warn 'Xray extraction failed'; return; }; rm -f "$zip"; chmod +x "$XRAY" 2>/dev/null || true; ok 'Xray installed.'; else rm -f "$zip"; warn 'Xray download failed.'; fi
}

install_mtproxy(){
  [[ -x "$MTPROXY" ]] && { ok 'MTProxy already installed.'; return; }; local t b; t=$(mktemp -d /tmp/mtproxy.XXXXXX); log 'Building MTProxy...';
  if ! git clone --depth 1 https://github.com/TelegramMessenger/MTProxy.git "$t/MTProxy"; then rm -rf "$t"; warn 'MTProxy download failed'; return; fi
  if ! make -C "$t/MTProxy" -j"$(nproc 2>/dev/null || echo 2)"; then rm -rf "$t"; warn 'MTProxy build failed'; return; fi
  b=''; [[ -x "$t/MTProxy/objs/bin/mtproto-proxy" ]] && b="$t/MTProxy/objs/bin/mtproto-proxy"; [[ -z "$b" && -x "$t/MTProxy/mtproto-proxy" ]] && b="$t/MTProxy/mtproto-proxy";
  [[ -n "$b" ]] || { rm -rf "$t"; warn 'MTProxy binary not found'; return; }; install -Dm755 "$b" "$MTPROXY"; rm -rf "$t"; ok 'MTProxy installed.'
}

create_service(){
  systemd_ok || return; cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=SpiderPanel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/uvicorn main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload; systemctl enable "$SERVICE.service" >/dev/null 2>&1 || true
}

systemd_ok(){ systemctl --quiet is-system-running >/dev/null 2>&1 || [[ "$(ps -p 1 -o comm= 2>/dev/null | tr -d '[:space:]')" == systemd ]]; }

running(){
  if systemd_ok; then systemctl is-active --quiet "$SERVICE.service"; return; fi
  [[ -f "$PIDFILE" ]] || return 1; local p; p=$(cat "$PIDFILE" 2>/dev/null || echo 0); [[ "$p" =~ ^[0-9]+$ ]] && kill -0 "$p" 2>/dev/null;
}

start_panel(){
  if systemd_ok; then create_service; systemctl restart "$SERVICE.service"; else
    running && { ok 'SpiderPanel already running.'; return; }; touch "$LOGFILE"; cd "$APP_DIR"; nohup "$VENV/bin/uvicorn" main:app --host 0.0.0.0 --port 8080 >> "$LOGFILE" 2>&1 & echo $! > "$PIDFILE";
  fi
  sleep 3; running || { error='SpiderPanel failed to start.'; printf '[ERROR] %s
' "$error" >&2; systemd_ok && journalctl -u "$SERVICE.service" -n 80 --no-pager || tail -n 80 "$LOGFILE"; return 1; }; ok 'SpiderPanel is running.'
}

stop_panel(){
  if systemd_ok; then systemctl stop "$SERVICE.service" || true; else if [[ -f "$PIDFILE" ]]; then kill "$(cat "$PIDFILE")" 2>/dev/null || true; rm -f "$PIDFILE"; fi; fi; ok 'SpiderPanel stopped.';
}

status_panel(){
  echo; echo 'SpiderPanel status'; echo '-----------------'; echo "OS: $OS_NAME"; echo 'Port: 8080'; [[ -x "$VENV/bin/python" ]] && echo "Python: $($VENV/bin/python --version 2>&1)" || echo 'Python: missing'; systemd_ok && echo 'Mode: systemd' || echo 'Mode: standalone'; running && echo 'Status: RUNNING' || echo 'Status: STOPPED'; echo;
}

get_public_ip(){ curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true; }
get_local_ip(){ hostname -I 2>/dev/null | awk '{print $1}' || true; }

info_panel(){
  local p ip lip; p=$(grep '^ADMIN_PASSWORD=' "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- || true); ip=$(get_public_ip); lip=$(get_local_ip);
  echo; echo '================================================'; echo 'SPIDERPANEL'; echo '================================================'; echo "URL: http://127.0.0.1:8080/spider"; [[ -n "$lip" ]] && echo "Local URL: http://$lip:8080/spider"; [[ -n "$ip" ]] && echo "Public URL: http://$ip:8080/spider";
  if [[ -n "${CODESPACE_NAME:-}" ]]; then echo "Codespace URL: https://${CODESPACE_NAME}-8080.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}/spider"; echo 'Forward port 8080 in Codespaces.'; fi
  echo "Admin Password: ${p:-NOT FOUND}"; echo "Application: $APP_DIR"; echo "Environment: $ENV_FILE"; echo "Python: $VENV/bin/python"; echo; echo 'Commands:'; echo '  spiderpanel'; echo '  spiderpanel info'; echo '  spiderpanel status'; echo '  spiderpanel start'; echo '  spiderpanel stop'; echo '  spiderpanel restart'; echo '  spiderpanel update'; echo '  spiderpanel logs'; echo '  spiderpanel uninstall'; echo '================================================'; echo;
}

logs_panel(){ if systemd_ok; then journalctl -u "$SERVICE.service" -f --no-pager; else touch "$LOGFILE"; tail -f "$LOGFILE"; fi; }

update_panel(){
  local f; f=$(mktemp /tmp/spiderpanel-update.XXXXXX); curl -fsSL --retry 5 "$INSTALLER_URL" -o "$f" || fail 'update download failed'; chmod 700 "$f"; bash "$f" install; rm -f "$f";
}

uninstall_panel(){
  echo; echo 'This will remove SpiderPanel.'; read -r -p 'Type REMOVE to continue: ' c; [[ "$c" == REMOVE ]] || { echo 'Cancelled.'; return; }; stop_panel || true; if systemd_ok; then systemctl disable "$SERVICE.service" >/dev/null 2>&1 || true; rm -f "/etc/systemd/system/$SERVICE.service"; systemctl daemon-reload || true; fi; rm -f "$CLI" "$ENV_FILE"; rm -rf "$APP_DIR"; ok 'SpiderPanel removed.'
}

create_cli(){
  cat > "$CLI" <<'EOF'
#!/usr/bin/env bash
set -e
APP=/opt/SpiderPanel
case "${1:-menu}" in
  info) bash "$APP/start.sh" info ;;
  status) bash "$APP/start.sh" status ;;
  start) bash "$APP/start.sh" start ;;
  stop) bash "$APP/start.sh" stop ;;
  restart) bash "$APP/start.sh" restart ;;
  update) bash "$APP/start.sh" update ;;
  logs) bash "$APP/start.sh" logs ;;
  uninstall) bash "$APP/start.sh" uninstall ;;
  *)
    echo; echo 'SpiderPanel'; echo;
    echo '1) Info'; echo '2) Status'; echo '3) Start'; echo '4) Stop'; echo '5) Restart'; echo '6) Update'; echo '7) Logs'; echo '8) Uninstall'; echo '0) Exit'; echo;
    read -r -p 'Select: ' n;
    case "$n" in
      1) bash "$APP/start.sh" info ;; 2) bash "$APP/start.sh" status ;; 3) bash "$APP/start.sh" start ;; 4) bash "$APP/start.sh" stop ;;
      5) bash "$APP/start.sh" restart ;; 6) bash "$APP/start.sh" update ;; 7) bash "$APP/start.sh" logs ;; 8) bash "$APP/start.sh" uninstall ;; 0) exit 0 ;; *) echo 'Invalid option.' ;;
    esac
    ;;
esac
EOF
  chmod 755 "$CLI"; ok 'Global command installed: spiderpanel'
}

install_panel(){
  root "$@"; detect; echo; log 'Installing SpiderPanel...'; log "OS: $OS_NAME"; log 'Port: 8080'; packages; download_repo; deploy; setup_python; install_xray || true; install_mtproxy || true; config; "$VENV/bin/python" -m compileall -q "$APP_DIR" || fail 'Python compile check failed'; cp -f "$TMP_ROOT/app/start.sh" "$APP_DIR/start.sh" 2>/dev/null || true; chmod 755 "$APP_DIR/start.sh" 2>/dev/null || true; create_cli; start_panel; info_panel; ok 'Installation completed.'
}

main(){
  case "${1:-install}" in
    install) install_panel "$@" ;;
    info) root "$@"; detect; info_panel ;;
    status) root "$@"; detect; status_panel ;;
    start) root "$@"; detect; start_panel ;;
    stop) root "$@"; detect; stop_panel ;;
    restart) root "$@"; detect; stop_panel; sleep 1; start_panel ;;
    update) root "$@"; update_panel ;;
    logs) root "$@"; detect; logs_panel ;;
    uninstall) root "$@"; detect; uninstall_panel ;;
    *) echo 'Usage: start.sh {install|info|status|start|stop|restart|update|logs|uninstall}'; exit 1 ;;
  esac
}

main "$@"
